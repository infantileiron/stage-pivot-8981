"""渠道费计费任务：提交冻结、冲突确认、原子发布、可恢复重试。"""
import os
from datetime import datetime,timezone
import redis
from fastapi import HTTPException
from sqlalchemy import func
from src.config.database import engine
from src.models.entities import Settlement,FeeContract,FeeRun,FeeRunMonth,FeeMonth,FeeAllocation
from src.services.business import dump
from src.services import fees
from src.services.locking import acquire,release,LEDGER_LOCK
QUEUE='stage:fee_runs'
def now():return datetime.now(timezone.utc).isoformat()
def enqueue(run_id:int):
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush(QUEUE,run_id)
 except redis.RedisError:raise HTTPException(503,'队列暂不可用，请恢复后新建计费任务')
def _live_settlements(s,months):
 return s.query(Settlement).filter(func.substr(Settlement.day,1,7).in_(months)).order_by(Settlement.id).all()
def _scopes(rows):
 def month(r):return r['day'][:7] if isinstance(r,dict) else r.day[:7]
 def channel(r):return r['channel'] if isinstance(r,dict) else r.channel
 return sorted({(month(r),channel(r)) for r in rows})
def build_freeze(s,months,take_over:bool):
 rows=[dump(r) for r in _live_settlements(s,months)]
 contracts=[dump(c) for c in s.query(FeeContract).order_by(FeeContract.id).all()]
 enabled=[c for c in contracts if c['status']=='enabled']
 def contract_of(channel,month):
  return next((c for c in enabled if c['channel']==channel and c['month_from']<=month<=c['month_to']),None)
 conflicts=[];scopes=[]
 for month,channel in _scopes(rows):
  scope_rows=[r for r in rows if r['day'][:7]==month and r['channel']==channel]
  c=contract_of(channel,month)
  item=dict(month=month,channel=channel,contract_id=c['id'] if c else None,contract_name=c['name'] if c else None,tiers=c['tiers'] if c else None,show_count=len(scope_rows))
  if c:
   manual=[r['code'] for r in scope_rows if (r['fee_cents'] or 0)>0]
   if manual:conflicts.append(dict(month=month,channel=channel,codes=manual))
  scopes.append(item)
 if conflicts and not take_over:
  raise HTTPException(409,{'message':'存在手填渠道费，需确认接管后才能由合同计费','conflicts':conflicts})
 freeze=dict(months=months,take_over=bool(take_over),settlements=rows,contracts=contracts,scopes=scopes)
 freeze['checksum_settlements']=fees.settlements_checksum(rows)
 freeze['checksum_contracts']=fees.contracts_checksum(s.query(FeeContract).order_by(FeeContract.id).all())
 return freeze,conflicts
def create_run(s,data):
 months=data.months
 freeze,conflicts=build_freeze(s,months,data.take_over)
 if not freeze['scopes']:raise HTTPException(422,'所选月份没有任何结算场次，无法计费')
 run=FeeRun(months=months,state='queued',freeze=freeze,checksum=freeze['checksum_settlements'],take_over=1 if data.take_over else 0,attempts=0,created=now(),updated=now())
 s.add(run);s.flush()
 for month,channel in _scopes(freeze['settlements']):
  s.add(FeeRunMonth(run_id=run.id,month=month,channel=channel,stale=0))
 s.commit()
 enqueue(run.id)
 return run
def compute_freeze(freeze:dict):
 """依据冻结快照重算全部适用月份，纯函数可重复，不产生外部副作用。"""
 rows=freeze['settlements']
 enabled=[c for c in freeze['contracts'] if c['status']=='enabled']
 def contract_of(channel,month):
  return next((c for c in enabled if c['channel']==channel and c['month_from']<=month<=c['month_to']),None)
 out=[]
 for month,channel in _scopes(rows):
  scope_rows=[r for r in rows if r['day'][:7]==month and r['channel']==channel]
  c=contract_of(channel,month)
  if not c:
   out.append(dict(month=month,channel=channel,contract_id=None,contract_name=None,tiers=None,allocations=[],total_base_cents=sum(r['gross_cents']-r['refund_cents'] for r in scope_rows),total_fee_cents=0,manual_total_cents=sum(r['fee_cents'] or 0 for r in scope_rows),show_count=len(scope_rows),no_contract=True))
   continue
  class _C:pass
  stub=_C();stub.channel=channel;stub.id=c['id'];stub.name=c['name'];stub.tiers=c['tiers']
  res=fees.allocate_month(scope_rows,stub);res['no_contract']=False
  out.append(res)
 return out
def publish_run(s,run:FeeRun):
 freeze=run.freeze
 if any(m.stale for m in s.query(FeeRunMonth).filter_by(run_id=run.id).all()):
  raise RuntimeError('受影响月份已被标记重算，结果已过期，请重试任务')
 live_rows=[dump(r) for r in _live_settlements(s,freeze['months'])]
 if fees.settlements_checksum(live_rows)!=freeze['checksum_settlements']:
  raise RuntimeError('结算源数据在计算期间被修订，过期结果拒绝落账，请重试任务')
 live_contracts=s.query(FeeContract).order_by(FeeContract.id).all()
 if fees.contracts_checksum(live_contracts)!=freeze['checksum_contracts']:
  raise RuntimeError('费率合同在计算期间被调整，过期结果拒绝落账，请重试任务')
 results=compute_freeze(freeze)
 if not acquire(s,engine,LEDGER_LOCK,timeout=0):
  raise RuntimeError('整月账本正在替换或透视取数中，请稍后重试')
 try:
  for res in results:
   if res.get('no_contract'):continue
   fm=s.query(FeeMonth).filter_by(month=res['month'],channel=res['channel']).first()
   if not fm:
    fm=FeeMonth(month=res['month'],channel=res['channel']);s.add(fm);s.flush()
   old=s.query(FeeAllocation).filter_by(fee_month_id=fm.id).all()
   for a in old:s.delete(a)
   fm.contract_id=res['contract_id'];fm.contract_name=res['contract_name'];fm.tiers=res['tiers']
   fm.total_base_cents=res['total_base_cents'];fm.total_fee_cents=res['total_fee_cents']
   fm.manual_total_cents=res['manual_total_cents'];fm.show_count=res['show_count']
   fm.run_id=run.id;fm.stale=0;fm.updated=now()
   fm.checksum=fees.checksum_payload(res['allocations'])
   s.flush()
   for a in res['allocations']:
    s.add(FeeAllocation(fee_month_id=fm.id,run_id=run.id,month=res['month'],channel=res['channel'],settlement_id=a['settlement_id'],code=a['code'],day=a['day'],ord_no=a['ord_no'],base_cents=a['base_cents'],gross_cents=a['gross_cents'],refund_cents=a['refund_cents'],manual_fee_cents=a['manual_fee_cents'],cum_base_cents=a['cum_base_cents'],cum_fee_cents=a['cum_fee_cents'],fee_cents=a['fee_cents']))
  s.commit()
 finally:
  release(s,engine,LEDGER_LOCK)
 return results
def retry_run(s,run:FeeRun):
 if run.state not in ('failed','done'):
  raise HTTPException(409,'仅失败或已发布的任务可以重试')
 freeze,conflicts=build_freeze(s,run.months,bool(run.take_over))
 if not freeze['scopes']:raise HTTPException(422,'所选月份已无结算场次，无法计费')
 run.freeze=freeze;run.checksum=freeze['checksum_settlements'];run.state='queued';run.error='';run.updated=now()
 s.query(FeeRunMonth).filter_by(run_id=run.id).delete()
 for month,channel in _scopes(freeze['settlements']):
  s.add(FeeRunMonth(run_id=run.id,month=month,channel=channel,stale=0))
 s.commit()
 enqueue(run.id)
 return run

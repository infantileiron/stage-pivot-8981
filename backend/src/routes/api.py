import os,json
from urllib.parse import quote
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from src.config.database import db,engine
from src.models.entities import User,Settlement,Pivot,Review,Audit,FeeContract,FeeRun,FeeMonth,FeeAllocation,FeeRunMonth
from src.schemas.business import Login,SettlementInput,PivotInput,ReviewInput,ReviewEdit,ContractInput,FeeRunInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.settlements import save,mark_dirty
from src.services.analysis import key
from src.services import fees
from src.services.fees import validate_tiers,month_range,overlaps
from src.services.fee_runs import create_run,retry_run,now
from src.services.locking import acquire,release,LEDGER_LOCK
from src.controllers.business import ensure_pivot,ensure_review,cell
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/settlements')
def settlements(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Settlement).order_by(Settlement.id).all()]
@router.post('/settlements')
def create(data:SettlementInput,u=Depends(editor),s=Depends(db)):
 r=save(s,data);audit(s,u,'录入结算 '+r['code']);return r
@router.put('/settlements/{i}')
def edit(i:int,data:SettlementInput,u=Depends(editor),s=Depends(db)):
 r=save(s,data,i);audit(s,u,'修订结算 '+str(i)+'，受影响月份已标记重算');return r
@router.get('/pivots')
def pivots(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Pivot).order_by(Pivot.id.desc()).all()]
@router.post('/pivots')
def submit(data:PivotInput,u=Depends(editor),s=Depends(db)):
 if not acquire(s,engine,LEDGER_LOCK,timeout=0):raise HTTPException(409,'渠道费整月账本正在原子替换，为保证取数一致请稍后再提交透视')
 try:
  rows=[dump(r) for r in s.query(Settlement).order_by(Settlement.id).all()]
 finally:
  release(s,engine,LEDGER_LOCK)
 r=Pivot(**data.model_dump(),result={'snapshot':rows});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('stage:pivots',r.id)
 except redis.RedisError:r.state='failed';r.error='队列暂不可用，请恢复后新建透视';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交透视 '+str(r.id));return dump(r)
@router.get('/pivots/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_pivot(s,i))
@router.get('/pivots/{i}/drill')
def drill(i:int,row_key:str,col_key:str,u=Depends(current),s=Depends(db)):
 r,c=cell(s,i,row_key,col_key)
 return {'cell':c,'rows':[x for x in r.result['snapshot'] if key(x,r.row_dim)==row_key and key(x,r.col_dim)==col_key]}
@router.get('/pivots/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_pivot(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功透视可以导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="stage-pivot-{i}.json"'})
@router.get('/reviews')
def reviews(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Review).order_by(Review.id.desc()).all()]
@router.post('/reviews')
def review(data:ReviewInput,u=Depends(editor),s=Depends(db)):
 run,c=cell(s,data.pivot_id,data.row_key,data.col_key)
 r=Review(**data.model_dump(),book_cents=c['net_cents'],difference_cents=data.expected_cents-c['net_cents']);s.add(r);s.commit();audit(s,u,'起草差异复核 '+str(r.id));return dump(r)
@router.put('/reviews/{i}')
def edit_review(i:int,data:ReviewEdit,u=Depends(editor),s=Depends(db)):
 r=ensure_review(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核复核单不可覆盖，请新建复核')
 r.expected_cents=data.expected_cents;r.difference_cents=data.expected_cents-r.book_cents;r.note=data.note;s.commit();audit(s,u,'修改差异复核 '+str(i));return dump(r)
@router.post('/reviews/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=ensure_review(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核复核单不可重复签核')
 r.status='approved';s.commit();audit(s,u,'签核差异复核 '+str(i));return dump(r)
def _contract(s,i):
 r=s.get(FeeContract,i)
 if not r:raise HTTPException(404,'费率合同不存在')
 return r
def _touch_contract_months(s,channel,month_from,month_to):
 for m in month_range(month_from,month_to):mark_dirty(s,channel,m)
@router.get('/fee-contracts')
def fee_contracts(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(FeeContract).order_by(FeeContract.id.desc()).all()]
@router.post('/fee-contracts')
def create_contract(data:ContractInput,u=Depends(editor),s=Depends(db)):
 tiers=validate_tiers([t.model_dump() for t in data.tiers])
 r=FeeContract(name=data.name,channel=data.channel,month_from=data.month_from,month_to=data.month_to,tiers=tiers,status='disabled',created=now(),updated=now())
 s.add(r);s.commit();audit(s,u,'新建费率合同 '+r.name+'（默认停用）');return dump(r)
@router.put('/fee-contracts/{i}')
def edit_contract(i:int,data:ContractInput,u=Depends(editor),s=Depends(db)):
 r=_contract(s,i);old=(r.channel,r.month_from,r.month_to,r.status)
 tiers=validate_tiers([t.model_dump() for t in data.tiers])
 if r.status=='enabled':
  hit=overlaps(s.query(FeeContract).all(),data.channel,data.month_from,data.month_to,self_id=i)
  if hit:raise HTTPException(409,f'与同月已启用合同《{hit.name}》生效区间重叠，禁止保存；请先停用冲突合同')
 r.name=data.name;r.channel=data.channel;r.month_from=data.month_from;r.month_to=data.month_to;r.tiers=tiers
 r.updated=now();s.commit()
 if old[3]=='enabled':
  _touch_contract_months(s,old[0],old[1],old[2])
  if (old[0],old[1],old[2])!=(r.channel,r.month_from,r.month_to):_touch_contract_months(s,r.channel,r.month_from,r.month_to)
 audit(s,u,'追溯调整费率合同 '+r.name+'，受影响月份已标记重算');return dump(r)
@router.post('/fee-contracts/{i}/enable')
def enable_contract(i:int,u=Depends(editor),s=Depends(db)):
 r=_contract(s,i)
 hit=overlaps(s.query(FeeContract).all(),r.channel,r.month_from,r.month_to,self_id=i)
 if hit:raise HTTPException(409,f'与同月已启用合同《{hit.name}》生效区间重叠，拒绝启用')
 r.status='enabled'
 r.updated=now();s.commit();_touch_contract_months(s,r.channel,r.month_from,r.month_to);audit(s,u,'启用费率合同 '+r.name);return dump(r)
@router.post('/fee-contracts/{i}/disable')
def disable_contract(i:int,u=Depends(editor),s=Depends(db)):
 r=_contract(s,i);r.status='disabled'
 r.updated=now();s.commit();_touch_contract_months(s,r.channel,r.month_from,r.month_to);audit(s,u,'停用费率合同 '+r.name);return dump(r)
@router.get('/fee-runs')
def fee_runs(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(FeeRun).order_by(FeeRun.id.desc()).all()]
@router.post('/fee-runs')
def submit_fee_run(data:FeeRunInput,u=Depends(editor),s=Depends(db)):
 r=create_run(s,data);audit(s,u,'提交渠道费计费任务 #'+str(r.id)+' 月份 '+','.join(data.months));return dump(r)
@router.get('/fee-runs/{i}')
def fee_run_detail(i:int,u=Depends(current),s=Depends(db)):
 r=s.get(FeeRun,i)
 if not r:raise HTTPException(404,'计费任务不存在')
 out=dump(r);out['run_months']=[dump(m) for m in s.query(FeeRunMonth).filter_by(run_id=i).order_by(FeeRunMonth.month,FeeRunMonth.channel).all()]
 return out
@router.post('/fee-runs/{i}/retry')
def fee_run_retry(i:int,u=Depends(editor),s=Depends(db)):
 r=s.get(FeeRun,i)
 if not r:raise HTTPException(404,'计费任务不存在')
 r=retry_run(s,r);audit(s,u,'重试渠道费计费任务 #'+str(i));return dump(r)
@router.get('/fee-months')
def fee_months(month:str|None=None,channel:str|None=None,u=Depends(current),s=Depends(db)):
 q=s.query(FeeMonth)
 if month:q=q.filter(FeeMonth.month==month)
 if channel:q=q.filter(FeeMonth.channel==channel)
 return [dump(r) for r in q.order_by(FeeMonth.month.desc(),FeeMonth.channel).all()]
@router.get('/fee-months/basis')
def fee_basis(month:str,channel:str,u=Depends(current),s=Depends(db)):
 r=s.query(FeeMonth).filter_by(month=month,channel=channel).first()
 if not r:raise HTTPException(404,'该渠道月份尚无已发布计费结果')
 rows=s.query(FeeAllocation).filter_by(fee_month_id=r.id).order_by(FeeAllocation.ord_no).all()
 basis=[]
 for a in rows:
  d=dump(a);d['marginal_tier']=fees.tier_label(r.tiers,a.cum_base_cents);basis.append(d)
 return {'month':dump(r),'basis':basis}
@router.get('/fee-months/export')
def fee_export(month:str,channel:str,u=Depends(current),s=Depends(db)):
 r=s.query(FeeMonth).filter_by(month=month,channel=channel).first()
 if not r:raise HTTPException(404,'该渠道月份尚无已发布计费结果')
 rows=s.query(FeeAllocation).filter_by(fee_month_id=r.id).order_by(FeeAllocation.ord_no).all()
 payload={'fee_month':dump(r),'basis':[dump(a) for a in rows],'method':'按月累计（票款−退款）阶梯分段，逐档向下取整分；逐场费用=当月累计费用之差，整数分严格守恒；场次按日期、编号排序。'}
 filename=f'fee-basis-{month}.json'
 disposition=f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote('fee-basis-'+month+'-'+channel+'.json')}"
 return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':disposition})
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]

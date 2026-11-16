"""阶梯渠道费计费引擎。全部金额为整数分，禁用浮点；分摊严格守恒。"""
import json,hashlib
from fastapi import HTTPException
def month_of(day:str)->str:
 return day[:7]
def shift_month(month:str,delta:int)->str:
 y,m=map(int,month.split('-'))
 n=(y*12+(m-1))+delta
 return f'{n//12:04d}-{n%12+1:02d}'
def month_range(month_from:str,month_to:str):
 if month_from>month_to:raise HTTPException(422,'生效起始月份不得晚于结束月份')
 out=[];cur=month_from
 while cur<=month_to:
  out.append(cur);cur=shift_month(cur,1)
 return out
def validate_tiers(tiers):
 """阶梯：[{from_cents, rate_bp}]，费率为基点（10000bp=100%），按累计票款净额分段。"""
 if not isinstance(tiers,list) or not tiers:raise HTTPException(422,'至少配置一档费率')
 last=-1
 for t in tiers:
  if set(t)!={'from_cents','rate_bp'}:raise HTTPException(422,'费率档仅允许 from_cents 与 rate_bp 字段')
  lo=t['from_cents'];bp=t['rate_bp']
  if not isinstance(lo,int) or not isinstance(bp,int) or lo<0 or not (0<=bp<=10000):raise HTTPException(422,'费率档下限须为非负整数分，费率基点须在0–10000之间')
  if lo<=last:raise HTTPException(422,'费率档下限必须严格递增且不重复')
  last=lo
 if tiers[0]['from_cents']!=0:raise HTTPException(422,'首档下限必须为0分')
 return [dict(from_cents=t['from_cents'],rate_bp=t['rate_bp']) for t in tiers]
def tier_fee(total_cents:int,tiers:list)->int:
 """月累计净额对应总费用：逐档按落在该档的金额乘以费率后向下取整分。"""
 fee=0
 for i,t in enumerate(tiers):
  upper=tiers[i+1]['from_cents'] if i+1<len(tiers) else None
  if upper is None:part=max(0,total_cents-t['from_cents'])
  else:part=max(0,min(total_cents,upper)-t['from_cents'])
  if part<=0:break
  fee+=part*t['rate_bp']//10000
 return fee
def tier_label(tiers:list,cum_cents:int)->str:
 label='0 分起'
 for t in tiers:
  if cum_cents>=t['from_cents']:label=f"{t['from_cents']} 分起 · {t['rate_bp']/10000:.4g}"
 return label
def contract_for(s,channel:str,month:str):
 from src.models.entities import FeeContract
 return s.query(FeeContract).filter(FeeContract.channel==channel,FeeContract.status=='enabled',FeeContract.month_from<=month,FeeContract.month_to>=month).order_by(FeeContract.id).first()
def overlaps(existing,channel:str,month_from:str,month_to:str,self_id=None):
 for c in existing:
  if c.id==self_id or c.channel!=channel or c.status!='enabled':continue
  if not (month_to<c.month_from or month_from>c.month_to):return c
 return None
def allocate_month(rows:list,contract)->dict:
 """rows 为同一渠道同一月份的场次（调用方负责过滤），按日期、编号排序后做累计差额分摊。"""
 ordered=sorted(rows,key=lambda r:(r['day'],r['code']))
 tiers=contract.tiers
 allocations=[];cum_base=0;cum_fee_prev=0
 for ord_no,r in enumerate(ordered,1):
  base=r['gross_cents']-r['refund_cents']
  cum_base+=base
  cum_fee=tier_fee(cum_base,tiers)
  fee=max(0,cum_fee-cum_fee_prev)
  allocations.append(dict(settlement_id=r['id'],code=r['code'],day=r['day'],ord_no=ord_no,base_cents=base,gross_cents=r['gross_cents'],refund_cents=r['refund_cents'],manual_fee_cents=r['fee_cents'] or 0,cum_base_cents=cum_base,cum_fee_cents=cum_fee,fee_cents=fee,marginal_tier=tier_label(tiers,cum_base)))
  cum_fee_prev=cum_fee
 total_base=sum(a['base_cents'] for a in allocations)
 total_fee=sum(a['fee_cents'] for a in allocations)
 manual_total=sum(a['manual_fee_cents'] for a in allocations)
 # 守恒断言：逐场分摊之和必须等于月累计总费用
 assert total_fee==(tier_fee(total_base,tiers) if allocations else 0)
 return dict(month=ordered[0]['day'][:7] if ordered else None,channel=contract.channel,contract_id=contract.id,contract_name=contract.name,tiers=tiers,allocations=allocations,total_base_cents=total_base,total_fee_cents=total_fee,manual_total_cents=manual_total,show_count=len(allocations))
def checksum_payload(payload)->str:
 return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def settlements_checksum(rows:list)->str:
 scope=[{k:r[k] for k in ('id','code','day','channel','gross_cents','refund_cents','fee_cents')} for r in rows]
 return checksum_payload(scope)
def contracts_checksum(contracts:list)->str:
 scope=[{k:getattr(c,k) for k in ('id','channel','month_from','month_to','tiers','status')} for c in contracts]
 return checksum_payload(scope)

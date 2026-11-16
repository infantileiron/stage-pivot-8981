from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Settlement
from src.services.business import dump
from src.services.fees import month_of
def mark_dirty(s,channel:str,month:str):
 """源数据或合同变动后，把已发布月份标为过期、把待算/计算中的任务月份标记重算。"""
 from src.models.entities import FeeMonth,FeeRunMonth,FeeRun
 s.query(FeeMonth).filter(FeeMonth.channel==channel,FeeMonth.month==month).update({FeeMonth.stale:1},synchronize_session=False)
 s.query(FeeRunMonth).filter(FeeRunMonth.channel==channel,FeeRunMonth.month==month,FeeRunMonth.stale==0).update({FeeRunMonth.stale:1},synchronize_session=False)
 run_ids=[x for (x,) in s.query(FeeRunMonth.run_id).filter(FeeRunMonth.channel==channel,FeeRunMonth.month==month).distinct().all()]
 if run_ids:s.query(FeeRun).filter(FeeRun.id.in_(run_ids),FeeRun.state=='queued').update({FeeRun.state:'failed',FeeRun.error:'源数据已修订，请确认后重试该计费任务'},synchronize_session=False)
def save(s,data,i=None):
 r=s.get(Settlement,i) if i else Settlement()
 if not r:raise HTTPException(404,'结算场次不存在')
 old_channel,old_day=(r.channel,r.day) if i else (None,None)
 d=data.model_dump();d['day']=d['day'].isoformat()
 for k,v in d.items():setattr(r,k,v)
 s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'场次编号重复，一场次仅一条结算')
 new_month=month_of(r.day)
 mark_dirty(s,r.channel,new_month)
 if old_channel and (old_channel!=r.channel or old_day!=r.day):mark_dirty(s,old_channel,month_of(old_day))
 s.commit()
 return dump(r)

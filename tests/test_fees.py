"""渠道阶梯费率：引擎、服务与 API 的端到端测试（SQLite + 假 Redis）。"""
import os,sys,json,threading,time,tempfile
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','backend'))
_DB=os.path.join(tempfile.mkdtemp(prefix='stage-test-'),'test.db')
os.environ['DATABASE_URL']='sqlite:///'+_DB
os.environ['ARTIFACT_DIR']=tempfile.mkdtemp(prefix='stage-artifacts-')
os.environ['REDIS_URL']='redis://fake/0'
os.environ['JWT_SECRET']='test-secret-3112'

class FakeRedis:
 _shared=None
 _shared_lock=threading.Lock()
 @classmethod
 def from_url(cls,*a,**k):
  with cls._shared_lock:
   if cls._shared is None:cls._shared=cls()
   return cls._shared
 def __init__(self,*a,**k):self.queues={};self.lock=threading.Lock()
 def rpush(self,k,*v):
  with self.lock:self.queues.setdefault(k,[]).extend(v)
 def lpop(self,k):
  with self.lock:return self.queues.get(k,[None]).pop(0) if self.queues.get(k) else None
 def blpop(self,keys,timeout=0):
  deadline=time.time()+timeout
  while True:
   with self.lock:
    for k in keys:
     if self.queues.get(k):return k,self.queues[k].pop(0)
   if time.time()>deadline:return None
   time.sleep(0.01)
 def ping(self):return True
import redis as _redis
_redis.Redis=FakeRedis
_redis.RedisError=RuntimeError

import pytest
from fastapi.testclient import TestClient
from src.config.database import Base,engine,Session
from src.models import entities
from src.models.entities import Settlement,FeeContract,FeeRun,FeeMonth,FeeAllocation,FeeRunMonth
from src.utils.security import hash_password
from src.services import fees
from src.services.fees import tier_fee
from src.services import fee_runs
from src.services.fee_runs import create_run,publish_run,retry_run,compute_freeze,QUEUE
from src.services.locking import acquire,release,LEDGER_LOCK
from src.main import app
client=TestClient(app)

def setup_db():
 Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
 with Session() as s:
  s.add(entities.User(email='admin@demo.local',role='admin',password=hash_password('Demo123!')))
  s.add(entities.User(email='analyst@demo.local',role='analyst',password=hash_password('Demo123!')))
  s.add(entities.User(email='viewer@demo.local',role='viewer',password=hash_password('Demo123!')))
  s.commit()

def token(role='analyst'):
 r=client.post('/auth/login',json={'email':f'{role}@demo.local','password':'Demo123!'})
 return r.json()['token']
def auth(role='analyst'):return {'Authorization':'Bearer '+token(role)}

def worker_finish(run_id):
 """模拟 worker：queued→running→发布→done/失败可恢复。"""
 with Session() as s:
  run=s.get(FeeRun,run_id)
  if run.state!='queued':return run.state
  run.state='running';run.attempts=(run.attempts or 0)+1;s.commit()
  try:publish_run(s,run);run=s.get(FeeRun,run_id);run.state='done';run.result={'months':True};s.commit()
  except Exception:
   s.rollback();run=s.get(FeeRun,run_id);run.state='failed';s.commit();raise
  return run.state

def mk_settlement(s,code,day,channel,gross,refund=0,fee=0,venue='云幕大剧场',play='渡口来信',seats=100,checked=80):
 s.add(Settlement(code=code,day=day,venue=venue,play=play,channel=channel,gross_cents=gross,refund_cents=refund,fee_cents=fee,seats=seats,checked=checked));s.commit()

# ---------- 1. 纯算法：分段取整与守恒 ----------
def test_tier_fee_pure_math():
 tiers=[{'from_cents':0,'rate_bp':500},{'from_cents':100000,'rate_bp':300},{'from_cents':300000,'rate_bp':100}]
 # 150000: 100000*5%=5000; 50000*3%=1500 →6500
 assert tier_fee(150000,tiers)==6500
 # 350000: 5000+6000+50000*1%=500 →11500
 assert tier_fee(350000,tiers)==11500
 # 跨档边界
 assert tier_fee(0,tiers)==0
 assert tier_fee(100000,tiers)==5000
 assert tier_fee(99999,tiers)==99999*500//10000
 # 零费率档
 assert tier_fee(999999999,[{'from_cents':0,'rate_bp':0}])==0

def test_allocation_integer_conservation():
 class C:pass
 c=C();c.channel='剧院直营';c.id=1;c.name='测试合同'
 c.tiers=[{'from_cents':0,'rate_bp':700},{'from_cents':200000,'rate_bp':300}]
 rows=[
  {'id':1,'code':'YM-001','day':'2026-09-02','gross_cents':120000,'refund_cents':20000,'fee_cents':0},
  {'id':2,'code':'YM-002','day':'2026-09-01','gross_cents':100000,'refund_cents':0,'fee_cents':0},
  {'id':3,'code':'YM-003','day':'2026-09-02','gross_cents':50000,'refund_cents':0,'fee_cents':0},
 ]
 res=fees.allocate_month(rows,c)
 # 排序：09-01 YM-002 → 09-02 YM-001 → 09-02 YM-003
 assert [a['code'] for a in res['allocations']]==['YM-002','YM-001','YM-003']
 assert [a['ord_no'] for a in res['allocations']]==[1,2,3]
 # 累计基数：100000 → 200000 → 250000
 assert [a['cum_base_cents'] for a in res['allocations']]==[100000,200000,250000]
 # 累计费用：7000 →14000 →14000+50000*3%=15500
 assert [a['cum_fee_cents'] for a in res['allocations']]==[7000,14000,15500]
 # 差额分摊
 assert [a['fee_cents'] for a in res['allocations']]==[7000,7000,1500]
 assert sum(a['fee_cents'] for a in res['allocations'])==res['total_fee_cents']==tier_fee(250000,c.tiers)
 assert res['total_base_cents']==250000

def test_allocation_rounding_remainder_conserved():
 # 构造每场均产生分币余数的场景，验证逐场差额定能守恒
 class C:pass
 c=C();c.channel='票务合作';c.id=2;c.name='余数合同';c.tiers=[{'from_cents':0,'rate_bp':333}]
 rows=[{'id':i,'code':f'YM-{i:03}','day':f'2026-09-{(i%9)+1:02}','gross_cents':1000,'refund_cents':0,'fee_cents':0} for i in range(1,13)]
 res=fees.allocate_month(rows,c)
 assert sum(a['fee_cents'] for a in res['allocations'])==tier_fee(12000,c.tiers)
 assert all(a['fee_cents']>=0 for a in res['allocations'])

# ---------- 2. 合同校验与重叠 ----------
def test_tier_validation():
 import pytest
 from fastapi import HTTPException
 for bad in ([],[{'from_cents':10,'rate_bp':100}], [{'from_cents':0,'rate_bp':-1}], [{'from_cents':0,'rate_bp':100},{'from_cents':0,'rate_bp':200}], [{'from_cents':0,'rate_bp':20000}]):
  with pytest.raises(HTTPException):fees.validate_tiers(bad)
 assert fees.validate_tiers([{'from_cents':0,'rate_bp':500}])==[{'from_cents':0,'rate_bp':500}]

def test_contract_overlap_rejected():
 setup_db()
 h={'Authorization':'Bearer '+token()}
 body={'name':'直营九月合同','channel':'剧院直营','month_from':'2026-09','month_to':'2026-12','tiers':[{'from_cents':0,'rate_bp':500}]}
 r=client.post('/fee-contracts',json=body,headers=h);assert r.status_code==200;cid=r.json()['id']
 assert r.json()['status']=='disabled'
 assert client.post(f'/fee-contracts/{cid}/enable',headers=h).status_code==200
 body2={'name':'直营重叠合同','channel':'剧院直营','month_from':'2026-10','month_to':'2026-11','tiers':[{'from_cents':0,'rate_bp':400}]}
 r=client.post('/fee-contracts',json=body2,headers=h);c2=r.json()['id']
 r=client.post(f'/fee-contracts/{c2}/enable',headers=h)
 assert r.status_code==409 and '重叠' in r.json()['detail']
 # 相邻不重叠可启用
 body3={'name':'直营次年合同','channel':'剧院直营','month_from':'2027-01','month_to':'2027-03','tiers':[{'from_cents':0,'rate_bp':400}]}
 c3=client.post('/fee-contracts',json=body3,headers=h).json()['id']
 assert client.post(f'/fee-contracts/{c3}/enable',headers=h).status_code==200
 # 不同渠道同月可并存
 body4={'name':'票务九月合同','channel':'票务合作','month_from':'2026-09','month_to':'2026-12','tiers':[{'from_cents':0,'rate_bp':300}]}
 c4=client.post('/fee-contracts',json=body4,headers=h).json()['id']
 assert client.post(f'/fee-contracts/{c4}/enable',headers=h).status_code==200
 # 停用后重叠合同可启用
 assert client.post(f'/fee-contracts/{cid}/disable',headers=h).status_code==200
 assert client.post(f'/fee-contracts/{c2}/enable',headers=h).status_code==200

# ---------- 3. 任务生命周期：待算→发布，幂等重试 ----------
def seed_two_channels(s):
 mk_settlement(s,'YM-001','2026-09-10','剧院直营',100000,0)
 mk_settlement(s,'YM-002','2026-09-15','剧院直营',150000,10000)
 mk_settlement(s,'YM-003','2026-09-20','票务合作',80000,0)

def test_run_full_lifecycle_and_publish():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500},{'from_cents':100000,'rate_bp':300}],status='enabled'))
  s.add(FeeContract(name='票务合同',channel='票务合作',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':300}],status='enabled'))
  s.commit()
 h={'Authorization':'Bearer '+token()}
 r=client.post('/fee-runs',json={'months':['2026-09']},headers=h);assert r.status_code==200
 run_id=r.json()['id']
 with Session() as s:
  run=s.get(FeeRun,run_id);assert run.state=='queued'
 assert worker_finish(run_id)=='done'
 with Session() as s:
  run=s.get(FeeRun,run_id);assert run.state=='done'
  # 整月账本
  fm=s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one()
  assert fm.total_base_cents==240000 and fm.show_count==2 and fm.stale==0
  # 直营：基数 100000(费5000) → 240000(费 5000+140000*3%=4200 →9200)
  allocs={a.code:a for a in s.query(FeeAllocation).filter_by(fee_month_id=fm.id)}
  assert allocs['YM-001'].fee_cents==5000 and allocs['YM-002'].fee_cents==4200
  assert fm.total_fee_cents==9200
  fm2=s.query(FeeMonth).filter_by(month='2026-09',channel='票务合作').one()
  assert fm2.total_fee_cents==80000*300//10000
 # 分档依据接口
 r=client.get('/fee-months/basis',params={'month':'2026-09','channel':'剧院直营'},headers=h)
 assert r.status_code==200 and len(r.json()['basis'])==2
 # 首场累计100000，正好跨入第二档；第二场累计240000仍在第二档
 assert r.json()['basis'][0]['marginal_tier']=='100000 分起 · 0.03'
 assert r.json()['basis'][1]['marginal_tier']=='100000 分起 · 0.03'
 # 导出
 r=client.get('/fee-months/export',params={'month':'2026-09','channel':'剧院直营'},headers=h)
 assert r.status_code==200 and r.headers['content-disposition'].startswith('attachment; filename="fee-basis-2026-09.json"')
 payload=r.json();assert payload['fee_month']['total_fee_cents']==9200

def test_retry_idempotent_no_double_charge():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})())
  rid=run.id;publish_run(s,run)
 with Session() as s:
  before=s.query(FeeAllocation).count()
  run=s.get(FeeRun,rid)
  run.state='done';s.commit()  # 已发布任务允许重算
  retry_run(s,run)
  run=s.get(FeeRun,rid);assert run.state=='queued'
  publish_run(s,run)
  after=s.query(FeeAllocation).count()
  assert before==after  # 原子替换，不叠加
  fm=s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one()
  assert fm.total_fee_cents==240000*500//10000
  assert s.query(FeeRun).count()==1  # 仍是同一任务，不新建

# ---------- 4. 手填费用冲突与确认接管 ----------
def test_manual_fee_takeover():
 setup_db()
 with Session() as s:
  mk_settlement(s,'YM-001','2026-09-10','剧院直营',100000,0,fee=7777)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 h={'Authorization':'Bearer '+token()}
 r=client.post('/fee-runs',json={'months':['2026-09']},headers=h)
 assert r.status_code==409
 detail=r.json()['detail']
 assert detail['conflicts'][0]['codes']==['YM-001']
 # 确认接管
 r=client.post('/fee-runs',json={'months':['2026-09'],'take_over':True},headers=h)
 assert r.status_code==200;rid=r.json()['id']
 assert worker_finish(rid)=='done'
 with Session() as s:
  fm=s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one()
  a=s.query(FeeAllocation).filter_by(fee_month_id=fm.id).one()
  assert a.manual_fee_cents==7777  # 手填值留痕
  assert a.fee_cents==5000         # 合同计费接管
  # 原始结算行的手填值保留（历史可追溯）
  assert s.get(Settlement,a.settlement_id).fee_cents==7777
  assert fm.manual_total_cents==7777

# ---------- 5. 过期结果拒绝落账（计算期间修订源数据） ----------
def test_stale_result_rejected():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());rid=run.id
 # 模拟计算期间另一会话修订金额
 with Session() as s:
  r=s.query(Settlement).filter_by(code='YM-001').one();r.gross_cents=120000;s.commit()
 with Session() as s:
  run=s.get(FeeRun,rid)
  run.state='running';s.commit()
  with pytest.raises(RuntimeError,match='过期结果拒绝落账'):publish_run(s,run)
  assert s.get(FeeRun,rid).state=='running'
  assert s.query(FeeMonth).count()==0
 # 重试成功（worker 启动恢复后失败态由 API 重试）
 with Session() as s:
  run=s.get(FeeRun,rid);run.state='failed';s.commit()
  retry_run(s,run)
 assert worker_finish(rid)=='done'
 with Session() as s:
  assert s.get(FeeRun,rid).state=='done'
  assert s.get(FeeRun,rid).state=='done'
  assert s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one().total_base_cents==260000

def test_settlement_edit_marks_dirty():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());rid=run.id;publish_run(s,run)
 # 发布后改金额
 h={'Authorization':'Bearer '+token()}
 sid=client.get('/settlements',headers=h).json()[0]['id']
 body=client.get('/settlements',headers=h).json()[0];body['gross_cents']=110000;body['day']='2026-09-10'
 r=client.put(f'/settlements/{sid}',json=body,headers=h);assert r.status_code==200
 with Session() as s:
  fm=s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one();assert fm.stale==1
  rms=s.query(FeeRunMonth).filter_by(run_id=rid).all()
  assert any(m.stale==1 for m in rms)

def test_channel_switch_marks_both_months_dirty():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.add(FeeContract(name='票务合同',channel='票务合作',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':300}],status='enabled'))
  s.commit()
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());publish_run(s,run)
 h={'Authorization':'Bearer '+token()}
 rows=client.get('/settlements',headers=h).json()
 target=next(r for r in rows if r['code']=='YM-001')
 target['channel']='票务合作'
 assert client.put(f"/settlements/{target['id']}",json=target,headers=h).status_code==200
 with Session() as s:
  assert s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one().stale==1
  assert s.query(FeeMonth).filter_by(month='2026-09',channel='票务合作').one().stale==1

# ---------- 6. 合同追溯调整标记重算 ----------
def test_contract_retro_touch():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());publish_run(s,run)
 h={'Authorization':'Bearer '+token()}
 cid=client.get('/fee-contracts',headers=h).json()[0]['id']
 body=client.get('/fee-contracts',headers=h).json()[0]
 body['tiers']=[{'from_cents':0,'rate_bp':600}]
 assert client.put(f'/fee-contracts/{cid}',json=body,headers=h).status_code==200
 with Session() as s:
  assert s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one().stale==1

# ---------- 7. 权限与历史保留 ----------
def test_viewer_readonly_and_history_kept():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 hv={'Authorization':'Bearer '+token('viewer')}
 assert client.post('/fee-contracts',json={'name':'x','channel':'剧院直营','month_from':'2026-09','month_to':'2026-10','tiers':[{'from_cents':0,'rate_bp':1}]},headers=hv).status_code==403
 assert client.post('/fee-runs',json={'months':['2026-09']},headers=hv).status_code==403
 assert client.get('/fee-contracts',headers=hv).status_code==200
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());publish_run(s,run)
 # 历史 FeeRun/冻结保留
 assert client.get('/fee-runs',headers=hv).json()[0]['freeze'] is not None
 assert client.get('/fee-months',headers=hv).status_code==200

# ---------- 8. 无合同月份不产生费用 ----------
def test_month_without_contract():
 setup_db()
 with Session() as s:
  mk_settlement(s,'YM-009','2026-10-01','团体专场',50000,0,fee=100)
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-10'],'take_over':True})())
  results=publish_run(s,run)
  assert results[0]['no_contract'] is True and results[0]['total_fee_cents']==0
  assert s.query(FeeMonth).count()==0 and s.query(FeeAllocation).count()==0

# ---------- 9. 互斥锁 ----------
def test_lock_mutex():
 from src.services.locking import acquire,release,LEDGER_LOCK
 with Session() as s:
  assert acquire(s,engine,LEDGER_LOCK) is True
  assert acquire(s,engine,LEDGER_LOCK,timeout=0) is False
  release(s,engine,LEDGER_LOCK)
  assert acquire(s,engine,LEDGER_LOCK,timeout=0) is True
  release(s,engine,LEDGER_LOCK)

# ---------- 10. 并发发布：锁内原子替换 ----------
def test_concurrent_publish_serialized():
 setup_db()
 with Session() as s:
  seed_two_channels(s)
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 with Session() as s:
  r1=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})())
  r2=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})())
  ids=[r1.id,r2.id]
 errors=[]
 def worker(rid):
  try:
   with Session() as s:publish_run(s,s.get(FeeRun,rid))
  except Exception as e:errors.append(str(e))
 t1=threading.Thread(target=worker,args=(ids[0],));t2=threading.Thread(target=worker,args=(ids[1],))
 t1.start();t2.start();t1.join();t2.join()
 # 进程内锁：一个成功，另一个拿不到锁失败；数据仍一致
 with Session() as s:
  fm=s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one()
  assert s.query(FeeAllocation).filter_by(fee_month_id=fm.id).count()==2
  assert not errors or all('账本正在替换' in e for e in errors)

# ---------- 11. 透视取数与账本替换互斥 ----------
def test_pivot_blocked_while_ledger_locked():
 setup_db()
 h={'Authorization':'Bearer '+token()}
 with Session() as s:
  assert acquire(s,engine,LEDGER_LOCK) is True
  r=client.post('/pivots',json={'name':'锁内透视','row_dim':'venue','col_dim':'channel'},headers=h)
  assert r.status_code==409
  release(s,engine,LEDGER_LOCK)
 assert client.post('/pivots',json={'name':'锁后透视','row_dim':'venue','col_dim':'channel'},headers=h).status_code==200

# ---------- 12. 空月份与月份格式 ----------
def test_empty_month_rejected():
 setup_db()
 h={'Authorization':'Bearer '+token()}
 r=client.post('/fee-runs',json={'months':['2030-01']},headers=h)
 assert r.status_code==422
 r=client.post('/fee-runs',json={'months':['2026-13']},headers=h)
 assert r.status_code==422

if __name__=='__main__':
 sys.exit(pytest.main([__file__,'-v','-x']))

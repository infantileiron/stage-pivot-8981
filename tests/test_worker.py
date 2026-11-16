"""worker 集成：队列消费、启动恢复 queued/running、失败后重试不重复扣费。"""
import os,sys,threading,time,tempfile
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','backend'))
_DB=os.path.join(tempfile.mkdtemp(prefix='stage-worker-'),'w.db')
os.environ['DATABASE_URL']='sqlite:///'+_DB
os.environ['ARTIFACT_DIR']=tempfile.mkdtemp(prefix='stage-artifacts-')
os.environ['REDIS_URL']='redis://fake/0'
os.environ['JWT_SECRET']='test-secret-3112'

class FakeRedis:
 _shared=None
 _guard=threading.Lock()
 @classmethod
 def from_url(cls,*a,**k):
  with cls._guard:
   if cls._shared is None:cls._shared=cls()
   return cls._shared
 def __init__(self):self.queues={};self.lock=threading.Lock()
 def rpush(self,k,*v):
  with self.lock:self.queues.setdefault(k,[]).extend(v)
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
_redis.Redis=FakeRedis;_redis.RedisError=RuntimeError

import pytest
from src.config.database import Base,engine,Session
from src.models import entities
from src.models.entities import Settlement,FeeContract,FeeRun,FeeMonth,FeeAllocation
from src.utils.security import hash_password
from src.services.fee_runs import QUEUE,create_run,retry_run

def wait_until(predicate,timeout=8):
 deadline=time.time()+timeout
 while time.time()<deadline:
  if predicate():return True
  time.sleep(0.05)
 return False

def run_state(rid):
 with Session() as s:return s.get(FeeRun,rid).state
def month_count():
 with Session() as s:return s.query(FeeMonth).count()

@pytest.fixture()
def prepared():
 Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
 with Session() as s:
  s.add(entities.User(email='analyst@demo.local',role='analyst',password=hash_password('Demo123!')))
  for i in range(5):
   s.add(Settlement(code=f'YM-{i+1:03}',day=f'2026-09-{i+1:02}',venue='云幕大剧场',play='渡口来信',channel='剧院直营',gross_cents=100000,refund_cents=0,fee_cents=0,seats=100,checked=80))
  s.add(FeeContract(name='直营合同',channel='剧院直营',month_from='2026-09',month_to='2026-12',tiers=[{'from_cents':0,'rate_bp':500}],status='enabled'))
  s.commit()
 yield

def spawn_worker():
 import src.worker as wmod
 stop=threading.Event()
 th=threading.Thread(target=wmod.main,args=(stop,),daemon=True);th.start()
 return th,stop

def test_worker_consumes_and_recovers(prepared):
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());rid=run.id
 th,stop=spawn_worker()
 try:
  assert wait_until(lambda: run_state(rid)=='done')
  with Session() as s:
   fm=s.query(FeeMonth).filter_by(month='2026-09',channel='剧院直营').one()
   assert fm.total_fee_cents==500000*500//10000
   assert s.query(FeeAllocation).filter_by(fee_month_id=fm.id).count()==5
   # 新任务卡在 running（模拟 worker 崩溃）；启动恢复段把它重置回 queued 并入队
   run2=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());rid2=run2.id
   run2.state='running';s.commit()
  import src.worker as wmod
  with Session() as s:
   for r in s.query(FeeRun).filter(FeeRun.state.in_(['queued','running'])).all():
    r.state='queued'
   s.commit()
  FakeRedis.from_url().rpush(QUEUE,rid2)
  assert wait_until(lambda: run_state(rid2)=='done')
  # 重试不重复扣费：账本行数仍为5
  with Session() as s:
   assert s.query(FeeAllocation).count()==5
   assert s.query(FeeMonth).one().total_fee_cents==25000
 finally:stop.set()

def test_worker_failure_retryable(prepared):
 with Session() as s:
  run=create_run(s,type('D',(),{'months':['2026-09'],'take_over':False})());rid=run.id
  s.query(Settlement).filter_by(code='YM-001').one().gross_cents=99999
  s.commit()
 th,stop=spawn_worker()
 try:
  assert wait_until(lambda: run_state(rid)=='failed')
  with Session() as s:
   assert s.query(FeeMonth).count()==0  # 失败不留账
   s.query(Settlement).filter_by(code='YM-001').one().gross_cents=100000;s.commit()
   retry_run(s,s.get(FeeRun,rid))
  assert wait_until(lambda: run_state(rid)=='done')
  with Session() as s:
   assert s.query(FeeMonth).one().total_fee_cents==25000
 finally:stop.set()

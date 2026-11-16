"""回归：原有透视/复核流程 + 渠道费完整 HTTP 链路。"""
import os,sys,tempfile
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','backend'))
os.environ['DATABASE_URL']='sqlite:///'+os.path.join(tempfile.mkdtemp(prefix='stage-reg-'),'r.db')
os.environ['ARTIFACT_DIR']=tempfile.mkdtemp(prefix='stage-art-')
os.environ['REDIS_URL']='redis://fake/0'
os.environ['JWT_SECRET']='test-secret-3112'
class FakeRedis:
 _s=None
 @classmethod
 def from_url(cls,*a,**k):
  if cls._s is None:cls._s=cls()
  return cls._s
 def __init__(self):self.q={}
 def rpush(self,k,*v):self.q.setdefault(k,[]).extend(v)
 def blpop(self,*a,**k):return None
 def ping(self):return True
import redis as _redis
_redis.Redis=FakeRedis;_redis.RedisError=RuntimeError
from fastapi.testclient import TestClient
from src.main import app
from src.config.database import Base,engine,Session
from src.models import entities
from src.models.entities import Settlement,FeeContract,FeeRun,FeeMonth,FeeAllocation
from src.utils.security import hash_password
client=TestClient(app)
def setup():
 Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
 with Session() as s:
  for role in ('admin','analyst','viewer'):s.add(entities.User(email=f'{role}@demo.local',role=role,password=hash_password('Demo123!')))
  s.commit()
def H(role='analyst'):
 t=client.post('/auth/login',json={'email':f'{role}@demo.local','password':'Demo123!'}).json()['token']
 return {'Authorization':'Bearer '+t}
def test_existing_pivot_and_review_flow_regression():
 setup()
 h=H()
 body={'code':'YM-001','day':'2026-09-10','venue':'云幕大剧场','play':'渡口来信','channel':'剧院直营','gross_cents':100000,'refund_cents':10000,'fee_cents':5000,'seats':100,'checked':80}
 assert client.post('/settlements',json=body,headers=h).status_code==200
 r=client.post('/pivots',json={'name':'场馆渠道透视','row_dim':'venue','col_dim':'channel'},headers=h)
 assert r.status_code==200
 pid=r.json()['id']
 # 手动模拟 worker 计算
 from src.services.analysis import pivot as pivot_fn
 with Session() as s:
  p=s.get(entities.Pivot,pid);snap=p.result['snapshot'];p.result={**pivot_fn(snap,p.row_dim,p.col_dim),'snapshot':snap};p.state='done';s.commit()
 r=client.get(f'/pivots/{pid}',headers=h);assert r.json()['state']=='done'
 r=client.get(f'/pivots/{pid}/drill',params={'row_key':'云幕大剧场','col_key':'剧院直营'},headers=h)
 assert r.json()['rows'][0]['code']=='YM-001'
 rev=client.post('/reviews',json={'pivot_id':pid,'row_key':'云幕大剧场','col_key':'剧院直营','expected_cents':90000,'note':'回单晚入账，已核对凭据'},headers=h)
 assert rev.status_code==200 and rev.json()['difference_cents']==5000
 rid=rev.json()['id']
 assert client.post(f'/reviews/{rid}/approve',headers=H('admin')).status_code==200
 assert client.post(f'/reviews/{rid}/approve',headers=H('admin')).status_code==409
 assert client.get(f'/pivots/{pid}/export',headers=h).status_code==200
 # 权限回归
 assert client.post('/settlements',json=body,headers=H('viewer')).status_code==403
def test_full_fee_http_chain():
 setup()
 h=H()
 for i,(g,rf) in enumerate([(100000,0),(150000,10000),(80000,0)]):
  client.post('/settlements',json={'code':f'YM-{i+1:03}','day':f'2026-09-{10+i*5}','venue':'云幕大剧场','play':'渡口来信','channel':'剧院直营','gross_cents':g,'refund_cents':rf,'fee_cents':0,'seats':100,'checked':80},headers=h)
 # 建合同（默认停用）
 c=client.post('/fee-contracts',json={'name':'直营阶梯','channel':'剧院直营','month_from':'2026-09','month_to':'2026-12','tiers':[{'from_cents':0,'rate_bp':500},{'from_cents':100000,'rate_bp':300}]},headers=h)
 assert c.json()['status']=='disabled';cid=c.json()['id']
 assert client.post(f'/fee-contracts/{cid}/enable',headers=h).status_code==200
 # 校验错误入参
 bad=client.post('/fee-contracts',json={'name':'坏合同','channel':'剧院直营','month_from':'2026-09','month_to':'2026-12','tiers':[{'from_cents':10,'rate_bp':500}]},headers=h)
 assert bad.status_code==422
 # 提交计费
 r=client.post('/fee-runs',json={'months':['2026-09']},headers=h);assert r.status_code==200;rid=r.json()['id']
 assert client.get(f'/fee-runs/{rid}',headers=h).json()['run_months'][0]['stale']==0
 # 模拟 worker
 from src.services.fee_runs import publish_run
 with Session() as s:
  run=s.get(FeeRun,rid);run.state='running';s.commit();publish_run(s,run);run.state='done';s.commit()
 months=client.get('/fee-months',headers=h).json();assert len(months)==1 and months[0]['total_fee_cents']
 basis=client.get('/fee-months/basis',params={'month':'2026-09','channel':'剧院直营'},headers=h).json()
 assert [b['code'] for b in basis['basis']]==['YM-001','YM-002','YM-003']  # 日期排序
 assert sum(b['fee_cents'] for b in basis['basis'])==basis['month']['total_fee_cents']
 # 导出
 exp=client.get('/fee-months/export',params={'month':'2026-09','channel':'剧院直营'},headers=h)
 assert exp.status_code==200 and exp.json()['basis'][0]['ord_no']==1
 # 404 与权限
 assert client.get('/fee-months/basis',params={'month':'2030-01','channel':'剧院直营'},headers=h).status_code==404
 assert client.post('/fee-runs',json={'months':['2026-09']},headers=H('viewer')).status_code==403
 assert client.get('/fee-months',headers=H('viewer')).status_code==200
def test_seed_demo_runs_and_contracts_present():
 setup()
 from scripts.seed_demo import seed
 seed()
 with Session() as s:
  assert s.query(Settlement).count()==180
  assert s.query(FeeContract).filter_by(status='enabled').count()==3

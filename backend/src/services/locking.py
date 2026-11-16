"""渠道费整月原子替换与透视取数之间的互斥锁。"""
import threading
from sqlalchemy import text
_local_locks={}
_local_guard=threading.Lock()
def _is_mysql(engine):return engine.dialect.name=='mysql'
def acquire(s,engine,name:str,timeout:int=0)->bool:
 if _is_mysql(engine):
  return s.execute(text('SELECT GET_LOCK(:n,:t)'),{'n':name,'t':timeout}).scalar()==1
 with _local_guard:
  lk=_local_locks.setdefault(name,threading.Lock())
 return lk.acquire(blocking=False)
def release(s,engine,name:str):
 if _is_mysql(engine):
  s.execute(text('SELECT RELEASE_LOCK(:n)'),{'n':name})
 else:
  with _local_guard:
   lk=_local_locks.get(name)
  if lk and lk.locked():lk.release()
LEDGER_LOCK='stage:fee_ledger'

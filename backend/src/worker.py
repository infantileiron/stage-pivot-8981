import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import Pivot,FeeRun
from src.services.analysis import pivot
from src.services.fee_runs import publish_run,QUEUE
q=redis.Redis.from_url(os.environ['REDIS_URL'])
ARTIFACT_DIR=Path(os.environ.get('ARTIFACT_DIR','/artifacts'))
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
PIVOT_Q='stage:pivots'
with Session() as s:
 for r in s.query(Pivot).filter(Pivot.state.in_(['queued','running'])).all():r.state='queued';q.rpush(PIVOT_Q,r.id)
 for r in s.query(FeeRun).filter(FeeRun.state.in_(['queued','running'])).all():r.state='queued';q.rpush(QUEUE,r.id)
 s.commit()
def run_pivot(s,raw):
 r=s.get(Pivot,int(raw))
 if not r or r.state!='queued':return
 snapshot=r.result['snapshot'];r.state='running';s.commit()
 try:
  r.result={**pivot(snapshot,r.row_dim,r.col_dim),'snapshot':snapshot};r.state='done';(ARTIFACT_DIR/str(r.id)).with_suffix('.json').write_text(json.dumps(r.result,ensure_ascii=False))
 except Exception as e:r.state='failed';r.error=str(e)
 s.commit()
def run_fee(s,raw):
 r=s.get(FeeRun,int(raw))
 if not r or r.state!='queued':return
 r.state='running';r.attempts=(r.attempts or 0)+1;s.commit()
 try:
  results=publish_run(s,r)
  r.result={'months':results};r.state='done';r.error='';r.finished=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());(ARTIFACT_DIR/f'fee-run-{r.id}.json').write_text(json.dumps(r.result,ensure_ascii=False))
 except Exception as e:
  s.rollback();r=s.get(FeeRun,int(raw));r.state='failed';r.error=str(e);r.finished=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
 s.commit()
def main(stop_event=None):
 while stop_event is None or not stop_event.is_set():
  Path('/tmp/heartbeat').touch()
  s=Session()
  try:
   item=q.blpop([PIVOT_Q,QUEUE],timeout=3)
   if not item:continue
   name,raw=item
   if name==PIVOT_Q.encode():run_pivot(s,raw)
   else:run_fee(s,raw)
  except Exception:logging.exception('worker');time.sleep(2)
  finally:s.close()
if __name__=='__main__':main()

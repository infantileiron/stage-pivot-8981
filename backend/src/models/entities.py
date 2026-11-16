from sqlalchemy import Column,Integer,BigInteger,String,JSON,Text,ForeignKey,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Settlement(Base):
 __tablename__='settlements'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);day=Column(String(10));venue=Column(String(40));play=Column(String(60));channel=Column(String(30));gross_cents=Column(BigInteger);refund_cents=Column(BigInteger);fee_cents=Column(BigInteger);seats=Column(Integer);checked=Column(Integer)
class Pivot(Base):
 __tablename__='pivots'
 id=Column(Integer,primary_key=True);name=Column(String(100));row_dim=Column(String(30));col_dim=Column(String(30));state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
class Review(Base):
 __tablename__='reviews'
 id=Column(Integer,primary_key=True);pivot_id=Column(Integer,ForeignKey('pivots.id'));row_key=Column(String(100));col_key=Column(String(100));book_cents=Column(BigInteger);expected_cents=Column(BigInteger);difference_cents=Column(BigInteger);note=Column(Text);status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
class FeeContract(Base):
 __tablename__='fee_contracts'
 id=Column(Integer,primary_key=True);name=Column(String(100),nullable=False);channel=Column(String(30),nullable=False);month_from=Column(String(7),nullable=False);month_to=Column(String(7),nullable=False);tiers=Column(JSON,nullable=False);status=Column(String(20),default='disabled');created=Column(String(40));updated=Column(String(40))
class FeeRun(Base):
 __tablename__='fee_runs'
 id=Column(Integer,primary_key=True);months=Column(JSON,nullable=False);state=Column(String(20),default='queued');freeze=Column(JSON,nullable=True);result=Column(JSON,nullable=True);checksum=Column(String(64));error=Column(Text,default='');take_over=Column(Integer,default=0);attempts=Column(Integer,default=0);created=Column(String(40));updated=Column(String(40));finished=Column(String(40))
class FeeRunMonth(Base):
 __tablename__='fee_run_months'
 id=Column(Integer,primary_key=True);run_id=Column(Integer,ForeignKey('fee_runs.id'),nullable=False);month=Column(String(7),nullable=False);channel=Column(String(30),nullable=False);stale=Column(Integer,default=0)
 __table_args__=(UniqueConstraint('run_id','month','channel',name='uq_fee_run_month_channel'),)
class FeeMonth(Base):
 __tablename__='fee_months'
 id=Column(Integer,primary_key=True);month=Column(String(7),nullable=False);channel=Column(String(30),nullable=False);contract_id=Column(Integer);contract_name=Column(String(100));tiers=Column(JSON);total_base_cents=Column(BigInteger,default=0);total_fee_cents=Column(BigInteger,default=0);manual_total_cents=Column(BigInteger,default=0);show_count=Column(Integer,default=0);run_id=Column(Integer);checksum=Column(String(64));stale=Column(Integer,default=0);updated=Column(String(40))
 __table_args__=(UniqueConstraint('month','channel',name='uq_fee_month_channel'),)
class FeeAllocation(Base):
 __tablename__='fee_allocations'
 id=Column(Integer,primary_key=True);fee_month_id=Column(Integer,ForeignKey('fee_months.id'),nullable=False);run_id=Column(Integer,nullable=False);month=Column(String(7),nullable=False);channel=Column(String(30),nullable=False);settlement_id=Column(Integer,nullable=False);code=Column(String(60),nullable=False);day=Column(String(10));ord_no=Column(Integer);base_cents=Column(BigInteger);gross_cents=Column(BigInteger);refund_cents=Column(BigInteger);manual_fee_cents=Column(BigInteger,default=0);cum_base_cents=Column(BigInteger);cum_fee_cents=Column(BigInteger);fee_cents=Column(BigInteger)
 __table_args__=(UniqueConstraint('month','channel','settlement_id',name='uq_fee_allocation_settlement'),)

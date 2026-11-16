-- 渠道阶梯费率合同与计费账本（002）
CREATE TABLE fee_contracts (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(100) NOT NULL,
	channel VARCHAR(30) NOT NULL,
	month_from VARCHAR(7) NOT NULL,
	month_to VARCHAR(7) NOT NULL,
	tiers JSON NOT NULL,
	status VARCHAR(20),
	created VARCHAR(40),
	updated VARCHAR(40),
	PRIMARY KEY (id)
)

;


CREATE TABLE fee_runs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	months JSON NOT NULL,
	state VARCHAR(20),
	freeze JSON,
	result JSON,
	checksum VARCHAR(64),
	error TEXT,
	take_over INTEGER,
	attempts INTEGER,
	created VARCHAR(40),
	updated VARCHAR(40),
	finished VARCHAR(40),
	PRIMARY KEY (id)
)

;


CREATE TABLE fee_run_months (
	id INTEGER NOT NULL AUTO_INCREMENT,
	run_id INTEGER NOT NULL,
	month VARCHAR(7) NOT NULL,
	channel VARCHAR(30) NOT NULL,
	stale INTEGER,
	PRIMARY KEY (id),
	UNIQUE (run_id, month, channel),
	FOREIGN KEY(run_id) REFERENCES fee_runs (id)
)

;


CREATE TABLE fee_months (
	id INTEGER NOT NULL AUTO_INCREMENT,
	month VARCHAR(7) NOT NULL,
	channel VARCHAR(30) NOT NULL,
	contract_id INTEGER,
	contract_name VARCHAR(100),
	tiers JSON,
	total_base_cents BIGINT,
	total_fee_cents BIGINT,
	manual_total_cents BIGINT,
	show_count INTEGER,
	run_id INTEGER,
	checksum VARCHAR(64),
	stale INTEGER,
	updated VARCHAR(40),
	PRIMARY KEY (id),
	UNIQUE (month, channel)
)

;


CREATE TABLE fee_allocations (
	id INTEGER NOT NULL AUTO_INCREMENT,
	fee_month_id INTEGER NOT NULL,
	run_id INTEGER NOT NULL,
	month VARCHAR(7) NOT NULL,
	channel VARCHAR(30) NOT NULL,
	settlement_id INTEGER NOT NULL,
	code VARCHAR(60) NOT NULL,
	day VARCHAR(10),
	ord_no INTEGER,
	base_cents BIGINT,
	gross_cents BIGINT,
	refund_cents BIGINT,
	manual_fee_cents BIGINT,
	cum_base_cents BIGINT,
	cum_fee_cents BIGINT,
	fee_cents BIGINT,
	PRIMARY KEY (id),
	UNIQUE (month, channel, settlement_id),
	FOREIGN KEY(fee_month_id) REFERENCES fee_months (id)
)

;

/** @typedef {{id:number,code:string,day:string,venue:string,play:string,channel:string,gross_cents:number,refund_cents:number,fee_cents:number,seats:number,checked:number}} Settlement */
/** @typedef {{id:number,name:string,row_dim:string,col_dim:string,state:string,result:object}} Pivot */
/** @typedef {{id:number,pivot_id:number,row_key:string,col_key:string,book_cents:number,expected_cents:number,difference_cents:number,status:string,note:string}} Review */
/** @typedef {{id:number,name:string,channel:string,month_from:string,month_to:string,tiers:Array<{from_cents:number,rate_bp:number}>,status:string,created:string,updated:string}} FeeContract */
/** @typedef {{id:number,months:string[],state:string,freeze:object,result:object,error:string,take_over:number,attempts:number}} FeeRun */
/** @typedef {{id:number,month:string,channel:string,contract_id:number,contract_name:string,tiers:Array,total_base_cents:number,total_fee_cents:number,manual_total_cents:number,show_count:number,run_id:number,stale:number}} FeeMonth */
/** @typedef {{id:number,ord_no:number,code:string,day:string,base_cents:number,cum_base_cents:number,cum_fee_cents:number,fee_cents:number,manual_fee_cents:number,marginal_tier:string}} FeeAllocation */

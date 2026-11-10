/* Synthetic semantic evaluation; no corporate source. */
%let start='28FEB2024:00:00:00'dt;
%let finish='01APR2024:00:00:00'dt;
%macro summarize(active_flag=1);
proc sql;
create table mart.t10_result as
select g,intnx('month',datepart(observed_at),0,'b') as month format=yymmdd10.,
count(*) as n,sum(amount) as total
from source.events
where active=&active_flag and observed_at>=&start and observed_at<&finish
group by g,calculated month;
quit;
%mend;
%summarize(active_flag=1);
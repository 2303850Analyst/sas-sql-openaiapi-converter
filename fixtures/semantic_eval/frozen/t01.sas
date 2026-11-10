/* Synthetic semantic evaluation; no corporate source. */
data work.result(keep=id miss low truth row_sum ordinary);
set source.events;
miss=missing(x);
if x<0 then low=1; else low=0;
if x then truth=1; else truth=0;
row_sum=sum(x,y);
ordinary=x+y;
run;
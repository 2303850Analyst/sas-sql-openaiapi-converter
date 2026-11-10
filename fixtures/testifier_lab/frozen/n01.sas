/* Synthetic semantic evaluation; no corporate source. */
data mart.n01_result(keep=id flag);
set source.events;
if x=.A then flag=1; else flag=0;
run;
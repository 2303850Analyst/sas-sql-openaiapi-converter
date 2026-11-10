/* Synthetic semantic evaluation; no corporate source. */
data work.result(keep=id flag);
set source.events;
if x=.A then flag=1; else flag=0;
run;
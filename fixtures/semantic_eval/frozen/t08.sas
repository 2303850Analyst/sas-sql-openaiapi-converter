/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.events out=work.ordered;
by seq;
run;
data work.result(keep=id prev);
set work.ordered;
if active=1 then prev=lag(amount);
else prev=.;
run;
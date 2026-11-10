/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.events out=work.ordered;
by g seq;
run;
data work.result(keep=id g seq total);
set work.ordered;
by g seq;
if first.g then total=0;
total+amount;
run;
/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.events out=work.hi;
by k descending score seq;
run;
data mart.t03_top;
set work.hi;
by k descending score seq;
if first.k;
run;
proc sort data=source.events out=work.lo;
by k score seq;
run;
data mart.t03_bottom;
set work.lo;
by k score seq;
if first.k;
run;
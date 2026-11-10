/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.events out=work.result nodupkey equals;
by k;
run;
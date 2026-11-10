/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.events out=mart.n02_result nodupkey equals;
by k;
run;
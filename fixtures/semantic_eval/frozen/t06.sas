/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.lefts out=work.l;
by k id;
run;
proc sort data=source.rights out=work.r;
by k rseq;
run;
data work.result;
merge work.l(in=a) work.r(in=b);
by k;
if a;
keep k id age rvalue;
run;
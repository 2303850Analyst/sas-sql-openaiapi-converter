/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.lefts out=work.l;
by k lseq;
run;
proc sort data=source.rights out=work.r;
by k rseq;
run;
data mart.t05_result;
merge work.l(in=a) work.r(in=b);
by k;
if a;
keep k lseq lvalue rseq rvalue;
run;
/* Synthetic semantic evaluation; no corporate source. */
proc sort data=source.lefts out=work.l;
by k sensor id;
run;
proc sort data=source.rights out=work.r;
by k sensor;
run;
data mart.t04_result(keep=id amount bias adjusted);
merge work.l(in=a) work.r(in=b);
by k sensor;
if a;
adjusted=sum(amount,bias);
run;
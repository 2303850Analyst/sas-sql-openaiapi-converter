/* Synthetic semantic evaluation; no corporate source. */
data mart.t12_result(drop=old);
set source.firsts(rename=(x=old)) source.seconds(rename=(x=old));
value=sum(old,1);
run;
/* Synthetic semantic evaluation; no corporate source. */
data work.result(drop=old);
set source.firsts(rename=(x=old)) source.seconds(rename=(x=old));
value=sum(old,1);
run;
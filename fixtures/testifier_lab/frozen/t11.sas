/* Synthetic semantic evaluation; no corporate source. */
data mart.t11_result;
length code $5 short $3;
set source.events;
if id<0 or id>99999 then delete;
code=put(id,z5.);
short='ABCDEF';
run;
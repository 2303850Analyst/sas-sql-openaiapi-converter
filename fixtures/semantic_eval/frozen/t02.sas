/* Synthetic semantic evaluation; no corporate source. */
data work.result(keep=id miss equal_code marker);
length marker $30;
set source.events;
miss=missing(code);
if code='ABC' then equal_code=1; else equal_code=0;
marker='run; quit; proc sql;';
run;
/* Synthetic semantic evaluation; no corporate source. */
%let chosen=2;
%macro inner(mult=1);
data work.selected;
set source.events;
value=amount*&mult;
run;
%mend;
%macro outer(mode=1);
%if &mode=2 %then %do;
%inner(mult=3);
%end;
%else %do;
%inner(mult=7);
%end;
%mend;
%outer(mode=&chosen);
proc sql;
create table work.result as select g,count(*) as n,sum(value) as total
from work.selected group by g;
quit;
/* Synthetic semantic evaluation; no corporate source. */
data work.result(keep=id x y);
set source.events;
where x>=0;
x=x+1;
if x>=1;
if x<3 then y=10;
if y=10 then y=y+x;
run;
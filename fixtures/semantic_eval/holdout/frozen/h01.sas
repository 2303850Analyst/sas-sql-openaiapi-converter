/* Synthetic semantic evaluation; no corporate source. */
data work.before(keep=id amount stage) work.after(keep=id amount stage);
set source.events;
stage=1;
if amount<0 then output work.before;
amount=sum(amount,2);
stage=2;
if flag=1 then output work.after;
run;
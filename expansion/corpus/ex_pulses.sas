proc sort data=source.samples out=work.queued_pulses; by seq; run;
data mart.pulse_delta(keep=id previous); set work.queued_pulses; if enabled=1 then previous=lag(signal); else previous=.; run;
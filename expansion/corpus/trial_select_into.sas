%let cutoff=4; proc sql noprint; select points into :cutoff trimmed from source.parameters order by seq; quit;
data mart.selected_scores; set source.scores; if score>=&cutoff.; run;
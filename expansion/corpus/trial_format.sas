proc format; value badge 0='zero' 1-3='small' other='other'; run;
data mart.badges(keep=id label); length label $5; set source.levels; label=put(level,badge.); run;
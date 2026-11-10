data mart.catalogue; set source.editions; run;
proc append base=mart.catalogue data=source.accessions force; run;
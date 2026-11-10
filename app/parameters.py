"""Explicit scalar macro bindings. This is not an executable macro interpreter."""
import re
from datetime import date

def validate_parameters(value):
    if not isinstance(value,dict) or len(value)>100:raise ValueError('Параметры: ожидается объект не более 100 значений')
    result={}
    for name,v in value.items():
        if not re.fullmatch(r'[A-Za-z_]\w*',name):raise ValueError('Некорректное имя параметра')
        if isinstance(v,dict):
            if set(v)!={'type','value'} or v['type']!='date':raise ValueError('Типизированный параметр: поддержан type=date')
            try:date.fromisoformat(v['value'])
            except (TypeError,ValueError):raise ValueError('Дата параметра: YYYY-MM-DD') from None
        elif not isinstance(v,(str,int)) or isinstance(v,bool) or not re.fullmatch(r'[A-Za-z0-9_]+',str(v)) or len(str(v))>128:
            raise ValueError('Параметр: безопасный фрагмент имени/целое число либо объект date; произвольный SAS не выполняется')
        if name.casefold() in result:raise ValueError('Повторяющееся имя параметра')
        result[name.casefold()]=v
    return result

def expand(text,parameters):
    # Preserve SAS single-quoted strings and name literals. Double quotes expand.
    def replace(match):
        if match[1] is not None:return match[0]
        name=match[2].casefold()
        if name not in parameters:return match[0]
        v=parameters[name]
        if isinstance(v,dict):
            d=date.fromisoformat(v['value']);months='JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split()
            return f"'{d.day:02d}{months[d.month-1]}{d.year:04d}'d"
        return str(v)
    return re.sub(r"('(?:''|[^'])*')|&([A-Za-z_]\w*)\.?",replace,text)

"""Explicit, versioned subset of custom SAS VALUE formats.

No catalogue discovery or invented labels. FUZZ/MULTILABEL/PICTURE require
separate rules. User supplies definitions from an authorised local export.
"""
import math
import re
from sqlglot import exp

def validate_formats(value):
    if not isinstance(value,dict) or len(value)>100:raise ValueError('Форматы: ожидается объект не более 100 определений')
    result={}
    for name,spec in value.items():
        if not isinstance(name,str) or not re.fullmatch(r'\$?[A-Za-z_]\w*\.',name):raise ValueError('Формат: имя должно заканчиваться точкой')
        if not isinstance(spec,dict) or set(spec)-{'type','width','encoding','fuzz','entries','other'}:raise ValueError('Формат: неизвестные свойства; MULTILABEL/PICTURE не поддержаны')
        typ=spec.get('type');width=spec.get('width');encoding=spec.get('encoding');entries=spec.get('entries')
        if typ not in {'numeric','character'} or (name.startswith('$') != (typ=='character')):raise ValueError('Формат: тип и префикс $ должны совпадать')
        if type(width) is not int or not 1<=width<=32767 or encoding not in {'utf-8','cp1251'}:raise ValueError('Формат: нужны width 1–32767 и encoding utf-8/cp1251')
        if spec.get('fuzz')!=0:raise ValueError('Формат: поддержан только явно заданный fuzz: 0')
        if not isinstance(entries,list) or not 1<=len(entries)<=1000 or 'other' not in spec:raise ValueError('Формат: нужны entries (1–1000) и явная ветка other')
        def label(s):
            if not isinstance(s,str) or any(ord(c)<32 for c in s):raise ValueError('Формат: метка должна быть строкой без управляющих символов')
            try:n=len(s.encode(encoding))
            except UnicodeEncodeError:raise ValueError('Формат: метка не соответствует encoding') from None
            if n>width:raise ValueError('Формат: метка длиннее width; усечение требует отдельного правила')
            return s
        normalized=[];seen=set()
        for item in entries:
            if not isinstance(item,dict) or set(item)!={'value','label'}:raise ValueError('Формат: поддержаны точные value/label; диапазоны пока не поддержаны')
            v=item['value']
            if typ=='numeric':
                if v is not None and (type(v) not in {int,float} or not math.isfinite(v)):raise ValueError('Формат: числовое значение или null для обычного missing')
            elif not isinstance(v,str):raise ValueError('Формат: символьный value должен быть строкой')
            key=v.rstrip(' ') if isinstance(v,str) else v
            if key in seen:raise ValueError('Формат: повторяющееся значение')
            seen.add(key);normalized.append({'value':key,'label':label(item['label'])})
        key=name.casefold()
        if key in result:raise ValueError('Повторяющееся имя формата')
        result[key]={'type':typ,'width':width,'encoding':encoding,'fuzz':0,'entries':normalized,'other':label(spec['other'])}
    return result

def render_format(arg,spec,types):
    from .sas_functions import type_of
    if type_of(arg,types)!=spec['type']:raise ValueError('PUT пользовательского формата: подтвердите тип аргумента в column_types')
    def label(value):
        # PUT default alignment follows source type. Byte width is from SAS.
        padding=' '*(spec['width']-len(value.encode(spec['encoding'])))
        padded=padding+value if spec['type']=='numeric' else value+padding
        return exp.Literal.string(padded)
    branches=[]
    for entry in spec['entries']:
        v=entry['value']
        if v is None:predicate=exp.Is(this=arg.copy(),expression=exp.Null())
        elif spec['type']=='character':
            source=exp.Trim(this=exp.Coalesce(this=arg.copy(),expressions=[exp.Literal.string('')]),position='TRAILING')
            predicate=exp.EQ(this=source,expression=exp.Literal.string(v))
        else:predicate=exp.EQ(this=arg.copy(),expression=exp.Literal.number(v))
        branches.append(exp.If(this=predicate,true=label(entry['label'])))
    result=exp.Case(ifs=branches,default=label(spec['other']))
    result.meta.update(sas_type='character',sas_length=spec['width'])
    return result

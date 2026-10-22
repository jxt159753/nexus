"""Bounded JSON schemas for native plugins, without network references."""

import json
import math


def bounded(value, limit=32768):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > limit:
        raise ValueError('plugin JSON byte limit exceeded')
    def visit(item, depth=0):
        if depth > 12:
            raise ValueError('plugin JSON nesting limit exceeded')
        if isinstance(item, dict):
            if len(item) > 100 or not all(isinstance(k, str) for k in item):
                raise ValueError('bounded string-keyed object required')
            for child in item.values(): visit(child, depth+1)
        elif isinstance(item, list):
            if len(item) > 1000: raise ValueError('plugin array limit exceeded')
            for child in item: visit(child, depth+1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError('JSON value required')
    visit(value)
    return value


def schema(value, depth=0):
    if depth > 8 or not isinstance(value, dict):
        raise ValueError('bounded schema object required')
    if depth==0: bounded(value,16384)
    allowed = {'type','properties','required','additionalProperties','items','enum','const','minimum','maximum',
               'minLength','maxLength','minItems','maxItems','description','title','default','anyOf'}
    if set(value)-allowed:
        raise ValueError('unsupported plugin schema keyword')
    if 'anyOf' in value:
        if not isinstance(value['anyOf'],list) or not 1 <= len(value['anyOf']) <= 8:
            raise ValueError('bounded anyOf required')
        for child in value['anyOf']: schema(child,depth+1)
        return value
    kind=value.get('type')
    if kind not in {'object','array','string','integer','number','boolean','null'}:
        raise ValueError('supported schema type required')
    if kind=='object':
        props=value.get('properties',{})
        if not isinstance(props,dict) or len(props)>64 or not all(isinstance(k,str) and len(k)<=64 for k in props):
            raise ValueError('bounded schema properties required')
        for child in props.values(): schema(child,depth+1)
        required=value.get('required',[])
        if not isinstance(required,list) or not all(type(key) is str for key in required) or not set(required)<=set(props) or len(set(required))!=len(required):
            raise ValueError('required properties must be declared')
        extra=value.get('additionalProperties',False)
        if isinstance(extra,dict): schema(extra,depth+1)
        elif type(extra) is not bool: raise ValueError('invalid additionalProperties')
    if kind=='array': schema(value.get('items'),depth+1)
    for key in ('minLength','maxLength','minItems','maxItems'):
        if key in value and (type(value[key]) is not int or not 0<=value[key]<=32768):
            raise ValueError('invalid schema size bound')
    for key in ('minimum','maximum'):
        if key in value and (type(value[key]) not in (int,float) or type(value[key]) is float and not math.isfinite(value[key])):
            raise ValueError('invalid numeric bound')
    for low,high in (('minimum','maximum'),('minLength','maxLength'),('minItems','maxItems')):
        if low in value and high in value and value[low]>value[high]: raise ValueError('contradictory schema bounds')
    if 'enum' in value and (not isinstance(value['enum'],list) or not 1<=len(value['enum'])<=100):
        raise ValueError('bounded enum required')
    bounded(value,16384)
    return value


def validate(definition, value, path='$'):
    if 'anyOf' in definition:
        for choice in definition['anyOf']:
            try:
                validate(choice,value,path)
                return
            except ValueError: pass
        raise ValueError(path+': no schema alternative matched')
    kind=definition['type']
    types={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool,'null':type(None)}
    if kind=='number':
        if type(value) not in (int,float) or type(value) is float and not math.isfinite(value): raise ValueError(path+': finite number required')
    elif type(value) is not types[kind]: raise ValueError(path+': '+kind+' required')
    if 'const' in definition and (type(value) is not type(definition['const']) or value!=definition['const']): raise ValueError(path+': const mismatch')
    if 'enum' in definition and not any(type(value) is type(v) and value==v for v in definition['enum']): raise ValueError(path+': enum mismatch')
    if kind=='object':
        props=definition.get('properties',{})
        if not set(definition.get('required',[]))<=set(value): raise ValueError(path+': missing required property')
        for key,item in value.items():
            child=props.get(key,definition.get('additionalProperties',False))
            if child is False: raise ValueError(path+': unknown property '+key)
            if child is not True: validate(child,item,path+'.'+key)
    if kind=='array':
        if not definition.get('minItems',0)<=len(value)<=definition.get('maxItems',1000): raise ValueError(path+': array size')
        for index,item in enumerate(value): validate(definition['items'],item,f'{path}[{index}]')
    if kind=='string' and not definition.get('minLength',0)<=len(value)<=definition.get('maxLength',32768): raise ValueError(path+': string size')
    if kind in {'number','integer'} and not definition.get('minimum',-math.inf)<=value<=definition.get('maximum',math.inf): raise ValueError(path+': numeric range')


def checked(definition,value):
    bounded(value)
    validate(definition,value)
    return value

"""Associate receipts with exact action records already supplied to compression."""
import json

def decoded(value):
    if isinstance(value,str):
        try:return json.loads(value)
        except ValueError:return None
    return value

def has_action(record):
    for message in record.get('content',[]):
        if not isinstance(message,dict) or message.get('role')!='assistant':continue
        for call in message.get('tool_calls',[]):
            function=call.get('function',{})
            args=decoded(function.get('arguments'))
            if function.get('name')=='submit_executor_step' and isinstance(args,dict) and args.get('action'):
                return True
    return False

def receipt_steps(records):
    steps=set()
    for record in records:
        content=record.get('content',[])
        if not isinstance(content,list):continue
        for message in content:
            if not isinstance(message,dict) or message.get('role')!='user':continue
            value=decoded(message.get('content'))
            if isinstance(value,dict) and isinstance(value.get('action_result'),dict):
                step=value.get('step',record.get('step'))
                if isinstance(step,int):steps.add(step)
    return steps

def associated_actions(records,catalog):
    """No fuzzy claim search or future evidence; ambiguous step matches add nothing."""
    known={r['source'] for r in records};result=[]
    for step in sorted(receipt_steps(records)):
        candidates=[r for r in catalog if r.get('step')==step and has_action(r)]
        if len(candidates)==1 and candidates[0]['source'] not in known:
            result.append(candidates[0]);known.add(candidates[0]['source'])
    return result

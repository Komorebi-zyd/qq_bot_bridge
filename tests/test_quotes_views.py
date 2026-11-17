"""Quote context survives lite projection; ordinary messages stay small."""
from bridge.views import lean_message, lean_page, lean_outbound


def base(**kwargs):
    return dict(seq=42,user_id='user',content='这个呢',**kwargs)


def test_quoted_text_author_and_attachment():
    item=base(quote={'reference_index':'REFIDX_secret','seq':8,'elements':[
        {'content':'<@all> 看这里','author':{'user_id':'other','username':'小明','bot':False},
         'attachments':[{'content_type':'image/png','url':'https://cdn.qq.com/a.png','filename':'a.png','size':20}]}]})
    result=lean_message(item)
    assert result['text']=='这个呢'
    assert result['quote']=={'seq':8,'text':'[@全体成员] 看这里','from':{'id':'other','name':'小明'},
                             'attachments':[{'kind':'image','url':'https://cdn.qq.com/a.png','filename':'a.png','size':20}]}
    assert 'reference_index' not in str(result) and 'REFIDX_secret' not in str(result)


def test_normal_type103_context_is_not_quote():
    item=base(message_type=103,msg_elements=[{'content':'additional context'}])
    assert 'quote' not in lean_message(item)


def test_legacy_explicit_reference_gets_content():
    item=base(message_reference={'message_id':'REFIDX_old'},msg_elements=[{'content':'旧内容'}])
    assert lean_message(item)['quote']=={'text':'旧内容'}


def test_explicit_reference_without_snapshot_is_visible():
    item=base(quote={'reference_index':'REFIDX_unknown','elements':[]})
    assert lean_message(item)['quote']=={'available':False}


def test_nested_quote_and_truncation():
    item=base(quote={'key':'old-send','elements':[
        {'content':'父引用','elements':[{'content':'子引用','truncated':True}]}], 'truncated':True})
    result=lean_message(item)['quote']
    assert result['text']=='父引用' and result['elements'][0]['text']=='子引用'
    assert result['elements'][0]['truncated'] and result['truncated'] and result['key']=='old-send'


def test_outbound_quote_target_is_visible():
    entry=dict(history_seq=8,idempotency_key='new-send',status='sent',quote_to=42)
    assert lean_outbound(entry)['quote_to']==42
    entry=dict(history_seq=9,idempotency_key='new-send-2',status='sent',quote_key='old-send')
    assert lean_outbound(entry)['quote_key']=='old-send'


def test_attachment_budget_marker_is_not_a_fake_file():
    item=base(quote={'elements':[{'content':'截图','attachments':[
        {'content_type':'image/png','url':'https://cdn.qq.com/a.png'},
        {'truncated':True,'reason':'max_items'}]}]})
    quote=lean_message(item)['quote']
    assert quote['truncated'] is True
    assert len(quote['attachments'])==1 and quote['attachments'][0]['kind']=='image'

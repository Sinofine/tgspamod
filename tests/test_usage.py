import json
import unittest
import httpx
from moderator.llm import Classifier, ReviewUnavailable
from test_moderator import cfg

class UsageTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, usage, invalid=False):
        def handler(req):
            value='bad json' if invalid else json.dumps(dict(is_ad=False,confidence=.99,reason='正常',evidence=''))
            return httpx.Response(200,json={'usage':usage,'choices':[{'finish_reason':'stop','message':{'content':value}}]})
        c=Classifier(cfg(),httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with self.assertLogs('moderator',level='INFO') as logs:
                if invalid:
                    with self.assertRaises(ReviewUnavailable):await c.classify('message',{'text':'私人正文'})
                else:
                    self.assertFalse((await c.classify('message',{'text':'私人正文'})).is_ad)
            output='\n'.join(logs.output)
            self.assertNotIn('私人正文',output);self.assertNotIn('secret',output)
            return output
        finally:await c.close()
    async def test_deepseek_usage(self):
        log=await self.call({'prompt_tokens':1600,'completion_tokens':80,'total_tokens':1680,
                            'prompt_cache_hit_tokens':1408,'prompt_cache_miss_tokens':192})
        self.assertIn('input_tokens=1600 output_tokens=80 total_tokens=1680 cache_hit_tokens=1408 cache_miss_tokens=192',log)
    async def test_compatible_cached_tokens(self):
        log=await self.call({'prompt_tokens':1600,'prompt_tokens_details':{'cached_tokens':1408}})
        self.assertIn('cache_hit_tokens=1408 cache_miss_tokens=None',log)
    async def test_zero_is_not_missing(self):
        log=await self.call({'prompt_cache_hit_tokens':0,'prompt_tokens_details':{'cached_tokens':100}})
        self.assertIn('cache_hit_tokens=0',log)
    async def test_missing_and_invalid_usage_does_not_break_review(self):
        for usage in (None,[],{}, {'prompt_tokens':True,'completion_tokens':-1,'total_tokens':'forged\nlog',
                                  'prompt_tokens_details':'invalid'}):
            log=await self.call(usage)
            self.assertIn('input_tokens=None output_tokens=None total_tokens=None cache_hit_tokens=None cache_miss_tokens=None',log)
            self.assertNotIn('forged', next(line for line in log.splitlines() if 'llm_usage ' in line))
    async def test_usage_logged_even_when_verdict_invalid(self):
        log=await self.call({'prompt_tokens':20},invalid=True)
        self.assertIn('input_tokens=20',log)
    async def test_raw_response_logged_before_evidence_validation(self):
        content=json.dumps(dict(is_ad=True,confidence=.99,reason='广告',evidence='不存在'))
        body={'choices':[{'finish_reason':'stop','message':{'content':content,'reasoning_content':'思考\n第二行'}}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body))) as http:
            with self.assertLogs('moderator',level='INFO') as logs:
                with self.assertRaisesRegex(ReviewUnavailable,'ungrounded evidence'):
                    await Classifier(cfg(),http).classify('profile',{'bio':'原始内容'})
        row=next(x for x in logs.output if 'llm_response ' in x)
        self.assertNotIn('\n',row)
        self.assertEqual(json.loads(json.loads(row.split(' body=',1)[1])),body)
    async def test_malformed_response_and_key_redaction(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='bad json\nsecret'))) as http:
            with self.assertLogs('moderator',level='INFO') as logs:
                with self.assertRaisesRegex(ReviewUnavailable,'LLM invalid JSON'):
                    await Classifier(cfg(),http).classify('message',{'text':'hello'})
        row=next(x for x in logs.output if 'llm_response ' in x)
        self.assertNotIn('secret',row)
        self.assertEqual(json.loads(row.split(' body=',1)[1]),'bad json\n[REDACTED]')

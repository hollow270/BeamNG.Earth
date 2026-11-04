"""Bounded retries for immutable building inputs, not active-world replacements."""
import time

import buildingoverlaystore as fs

DELAY = 3.
LIMIT = 315.  # three existing read/publication windows; no source timer increase


class Pending:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.waiting={};self.age={}

    def defer(self,key,value):
        now=self.clock();first,count=self.age.get(key,(now,0))
        if now-first>=LIMIT:
            self.finish(key);return False
        self.age[key]=(first,count+1)
        self.waiting[key]=(now+min(15.,DELAY*2**min(count,3)),value)
        return True

    def ready(self,valid):
        now=self.clock();out=[]
        for key in list(self.age):
            if not valid(key):self.finish(key)
        for key,(when,value) in list(self.waiting.items()):
            if when<=now:self.waiting.pop(key);out.append(value)
        return out

    def delayed(self,key):return key in self.waiting
    def finish(self,key):self.waiting.pop(key,None);self.age.pop(key,None)
    def clear(self):self.waiting.clear();self.age.clear()
    def timeout(self):
        return max(0.,min(v[0] for v in self.waiting.values())-self.clock()) if self.waiting else None
    def __bool__(self):return bool(self.waiting)


def transient(error):return isinstance(error,fs.LockTimeout)

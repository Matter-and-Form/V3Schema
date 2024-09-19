import json
import websockets

class THREE:
    def __init__(self, url):
        self.url = url

    async def connect(self):
        self.ws = await websockets.connect(self.url)

    async def send(self, task):
        await self.ws.send(json.dumps({ "Task": task }))

        async for message in self.ws:
            try:
                response = json.loads(message)
                if 'Task' in response and 'State' in response['Task'] and response['Task']['Index'] == task['Index']:
                    if response['Task']['State'] == 'Failed':
                        raise Exception(response['Task'])
                    elif response['Task']['State'] == 'Completed':
                        return response['Task'].get('Output', {})
            except Exception as e:
                raise e

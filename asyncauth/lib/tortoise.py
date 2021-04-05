from sanic import Sanic
from tortoise import Tortoise

from asyncauth.core.config import config


async def initialize_tortoise(app: Sanic):
    """
    Initializes the tortoise-orm.
    """
    username = config['TORTOISE']['username']
    password = config['TORTOISE']['password']
    endpoint = config['TORTOISE']['endpoint']
    schema = config['TORTOISE']['schema']
    engine = config['TORTOISE']['engine']
    models = config['TORTOISE']['models'].split(',')
    url = engine + '://{0}:{1}@{2}/{3}'.format(username, password, endpoint, schema)
    await Tortoise.init(db_url=url, modules={"models": models})
    if config['TORTOISE']['generate'] == 'true':
        await Tortoise.generate_schemas()

    @app.listener("after_server_stop")
    async def close_orm(app, loop):
        await Tortoise.close_connections()



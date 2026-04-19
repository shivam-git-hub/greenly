from flask import Flask
from flask_cors import CORS

from routes.blueprints.chat import chat_bp


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    app.register_blueprint(chat_bp)

    return app

from dotenv import load_dotenv

load_dotenv()  # 只在本機讀 .env（.env 在 .gitignore 裡）；CI / 部署由 GitHub Secrets 注入環境變數

from app import create_app  # noqa: E402  要在 load_dotenv 之後 import，才讀得到環境變數

app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config["APP_ENV"] == "development", port=5000)

"""示範用的記憶體資料庫（負責：陳儀珊）。

之後換成 SQLite / PostgreSQL 時，只要維持同樣的屬性與方法即可。
其他組員需要的欄位（例如作答紀錄）可以直接加在這裡，PR 請標陳儀珊審核。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone

# RBAC 的三種角色。只有 beta_tester 會被分流到實驗組（分流邏輯在賴的 flags.py）
ROLES = ("student", "beta_tester", "admin")


@dataclass
class User:
    id: str
    name: str
    role: str                      # student / beta_tester / admin
    email: str = ""
    # 每次登出時 +1；session 裡的 epoch 對不上就視為失效（見 auth.load_user）。
    # 簽章 cookie 本身無法撤銷，這樣做可以讓「登出前被複製走的 cookie」也一起失效

    session_epoch: int = 0

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role}")


@dataclass
class Material:
    id: int
    title: str
    owner_id: str                  # ABAC 判斷用：講義只有擁有者能讀寫
    pages: list[str]               # 每頁文字，頁碼 = index + 1（ai_quiz 的「出自第 X 頁」用）
    filename: str | None = None    # 原始 PDF 檔名（只拿來顯示，檔案本身不保存）
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


SAMPLE_AI_WEEK3 = [
    "第 3 週：機器學習的三種學習方式。監督式學習使用有標籤的資料，例如用標好「垃圾信 / 正常信」的郵件訓練分類器。",
    "非監督式學習沒有標籤，目標是找出資料本身的結構，例如 K-means 分群。強化學習則是代理人透過獎勵與懲罰學習策略。",
    "過擬合（overfitting）是模型把訓練資料的雜訊也記住，導致在新資料上表現變差。常見對策：更多資料、正規化、交叉驗證。",
]

SAMPLE_BOB_NOTE = [
    "Bob 的私人筆記：梯度下降每次沿著損失函數的負梯度方向更新參數，學習率太大會發散，太小會收斂很慢。",
]

SAMPLE_BETTY = [
    "神經網路由多層神經元組成，每層做線性轉換再經過激活函數（ReLU、sigmoid）。",
    "反向傳播用連鎖律計算每個權重的梯度，再交給最佳化器（SGD、Adam）更新。",
]


class Store:
    def __init__(self) -> None:
        # 示範帳號：只在開發環境的示範登入使用；正式環境一律走 Google OIDC
        self.users: dict[str, User] = {
            "alice": User("alice", "Alice", "student", "alice@example.edu"),
            "bob": User("bob", "Bob", "student", "bob@example.edu"),
            "betty": User("betty", "Betty", "beta_tester", "betty@example.edu"),
            "ben": User("ben", "Ben", "beta_tester", "ben@example.edu"),
            "admin": User("admin", "Admin", "admin", "admin@example.edu"),
        }
        self._ids = itertools.count(1)
        self.materials: dict[int, Material] = {}
        self.add_material("AI 導論 第 3 週：學習方式與過擬合", "alice", SAMPLE_AI_WEEK3, "AI_week3.pdf")  # id 1
        self.add_material("Bob 的私人筆記：梯度下降", "bob", SAMPLE_BOB_NOTE)                         # id 2
        self.add_material("神經網路與反向傳播", "betty", SAMPLE_BETTY, "NN_basics.pdf")                # id 3

    # ---------- 講義 ----------
    def add_material(self, title: str, owner_id: str, pages: list[str],
                     filename: str | None = None) -> Material:
        m = Material(next(self._ids), title, owner_id, list(pages), filename)
        self.materials[m.id] = m
        return m

    def materials_of(self, user_id: str) -> list[Material]:
        return [m for m in self.materials.values() if m.owner_id == user_id]

    # ---------- 使用者 ----------
    def user_by_email(self, email: str) -> User | None:
        email = email.lower()
        return next((u for u in self.users.values() if u.email.lower() == email), None)

    def set_role(self, user_id: str, role: str) -> User:
        if role not in ROLES:
            raise ValueError(role)
        u = self.users[user_id]
        # session 只存 uid，角色每次請求都從這裡重新讀，所以改完下一個請求就生效
        u.role = role
        return u

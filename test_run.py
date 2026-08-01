from agent import TextToSQLAgent

def run():
    print("Initializing agent...")
    agent = TextToSQLAgent()
    print("Agent initialized successfully.")
    question = "目前資料庫裡面總共有幾位客戶？"
    print(f"Asking question: {question}")
    answer = agent.run(question)
    print("=" * 60)
    print("Answer:")
    print(answer)
    print("=" * 60)

if __name__ == "__main__":
    run()

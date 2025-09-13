# maat-memory-auto-for-textgen-ui
Automatic memory extension for Text-Generation-WebUI. Lets your AI store and inject contextual memories, featuring a multilingual user interface.

# 🧠 MAAT MemAuto  
Automatic Memory Extension for **Text-Generation-WebUI**

MAAT MemAuto adds **persistent, contextual memory** to Text-Generation-WebUI.  
The AI can now **create and store new memories on its own** and automatically inject relevant memories into prompts – fully multilingual and fully configurable.

---

## ✨ Key Features

### 🔄 Automatic Memory Handling
* The AI itself can **save memories autonomously** by outputting a `save:` command in its response.
* User-stored memories are automatically injected into the prompt when matching keywords are detected.
* Optional injection of current **time** and **date**.

### 🌍 Multilingual Interface
* Complete UI translations: **English, German, Spanish, French, Portuguese, Italian, Polish, Czech**.
* UI language can be changed at runtime (server restart recommended for full refresh).
* Custom guide text can be edited per language.

### 🧩 Flexible Management
* **Store memories** in three formats:
  * JSON: `save: {"memory":"…","keywords":"kw1,kw2","always":true}`
  * Key–Value: `save: memory=…, keywords=kw1,kw2, always=true`
  * Short form: `save: (short memory text)`
* **Edit / Delete**: Update or remove individual memories.
* **🧨 Delete ALL**: One-click deletion of all memories with an automatic backup.
* **Diagnostics Tab**: Shows recently injected memories and total injected characters.

### 🔒 Control & Safety
* Minimum-length and relevance filters keep memories meaningful.
* Automatic deduplication of identical entries.
* Optional toggle to disallow AI-initiated saves (`allow_model_saves`).
* All data is stored locally at  
  `user_data/maat_memauto/memories.json`.

---

## 🚀 Installation

1. **Clone or download**:
   ```bash
   git clone https://github.com/Chris4081/maat-memory-auto-for-textgen-ui.git

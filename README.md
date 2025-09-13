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
  * JSON:  
    ```text
    save: {"memory":"…","keywords":"kw1,kw2","always":true}
    ```
  * Key–Value:  
    ```text
    save: memory=…, keywords=kw1,kw2, always=true
    ```
  * Short form:  
    ```text
    save: (short memory text)
    ```
* **Edit / Delete**: Update or remove individual memories.
* **🧨 Delete ALL**: One-click deletion of all memories with an automatic backup.
* **Diagnostics Tab**: Shows recently injected memories and total injected characters.

### 🔒 Control & Safety
* Minimum-length and relevance filters keep memories meaningful.
* Automatic deduplication of identical entries.
* Optional toggle to disallow AI-initiated saves (`allow_model_saves`).
* All data is stored locally at  
  ```
  user_data/extensions/maat_memauto/memories.json
  ```

---

## 🚀 Installation

1. **Clone or download**:
   ```bash
   git clone https://github.com/Chris4081/maat-memory-auto-for-textgen-ui.git
   ```

2. **Copy the folder** into:
   ```
   text-generation-webui/user_data/extensions/
   ```

3. **Restart** Text-Generation-WebUI and enable the extension on the **Extensions** tab.

---

## 🖥️ Usage

1. Open the **MAAT MemAuto** tab inside the WebUI.  
2. Configure options under **⚙️ Settings**:
   * UI language *(save & restart the server to fully apply)*  
   * Time/date injection, guide injection, trigger words, etc.  
3. Add new memories or let the model create its own:
   ```text
   save: {"memory":"User prefers concise answers","keywords":"short","always":true}
   save: memory=No emojis, keywords=emoji, always=true
   save: (User enjoys dark mode)
   ```
4. The AI can also create memories autonomously by including, for example:
   ```text
   save: {"memory":"User enjoys science-fiction prompts","keywords":"sci-fi,story","always":false}
   ```

---

## 📜 License

Released under the **[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)**.  
See the `LICENSE` file for details.

> **Note:**  
> This extension was **inspired by and partially based on the idea and structure of**  
> [complex_memory](https://github.com/theubie/complex_memory).  
> **All code has been re-implemented independently.**

---

## 🛠️ Contributing
* Pull requests and new translations are welcome!  
* Please open a GitHub issue for bug reports or feature requests.

---

Enjoy **automatic, multilingual memory** in your Text-Generation-WebUI!

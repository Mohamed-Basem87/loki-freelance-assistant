"""
Drag-and-drop editor for app/keywords.py

Run:  python keyword_dnd_editor.py

- Drag a keyword item from one list and drop it onto another list to
  move it to that category.
- Double-click a keyword to edit its score (or delete it).
- Use the "+" button under a list to add a new keyword to that category.
- Click "Export keywords.py" to write the current state back out in the
  same format as the original file.

Only uses the standard library (tkinter) - nothing to install.
"""

import tkinter as tk
from tkinter import simpledialog, messagebox, filedialog

INTEREST_CATEGORIES = {
    "power_bi": {
        "power bi": 50, "powerbi": 50, "microsoft power bi": 50, "dashboard": 30,
        "dashboards": 30, "interactive dashboard": 35, "pbix": 60, "dax": 50,
        "measure": 20, "measures": 20, "calculated column": 20, "power query": 40,
        "power pivot": 35, "star schema": 40, "data model": 30, "data modeling": 30,
        "لوحة تحكم": 30, "لوحة معلومات": 30, "داشبورد": 30, "باور بي": 50,
    },
    "excel": {
        "excel": 30, "اكسل": 30, "إكسل": 30, "spreadsheet": 20, "workbook": 15,
        "worksheet": 15, "pivot table": 30, "pivot": 15, "power pivot": 30,
        "vlookup": 25, "xlookup": 25, "index match": 25, "lookup": 15,
        "remove duplicates": 20, "conditional formatting": 20, "financial model": 35,
        "financial modeling": 35, "google sheets": 20,
    },
    "sql": {
        "sql": 25, "mysql": 20, "postgresql": 20, "postgres": 20, "sqlite": 20,
        "oracle": 20, "sql server": 25, "database": 15, "query": 10, "queries": 10,
        "stored procedure": 15, "قاعدة بيانات": 20, "استعلام": 20,
    },
    "data_analysis": {
        "data analysis": 40, "data analyst": 40, "business analyst": 35,
        "business intelligence": 35, "analytics": 25, "analysis": 20,
        "financial analysis": 45, "financial analyst": 45, "sales analysis": 40,
        "marketing analysis": 35, "market analysis": 35, "customer analysis": 35,
        "hr analysis": 35, "forecast": 30, "forecasting": 30, "budget analysis": 35,
        "kpi": 25, "kpis": 25, "metrics": 20, "visualization": 25,
        "data visualization": 30, "report": 10, "reports": 10, "reporting": 15,
        "etl": 30, "data cleaning": 35, "تحليل بيانات": 40, "محلل بيانات": 40,
        "تحليل مالي": 45, "تحليل المبيعات": 40, "تحليل السوق": 35,
        "تحليل الأعمال": 35, "ذكاء الأعمال": 35, "تقارير": 15,
        "مؤشرات الأداء": 30, "تنظيف البيانات": 35, "التنبؤ": 30,
    },
    "python": {
        "python": 20, "automation": 20, "script": 15, "scripting": 15,
        "pandas": 30, "numpy": 25, "selenium": 20, "beautifulsoup": 20,
        "web scraping": 25, "scraping": 20, "etl": 20, "csv": 10, "json": 10,
        "api integration": 20,
    },
    "automation": {
        "automation": 30, "workflow": 25, "automate": 25, "bot": 20,
        "integration": 20, "make.com": 20, "n8n": 25, "zapier": 20,
    },
    "portfolio": {
        "portfolio": 35, "portfolio website": 50, "personal website": 40,
        "landing page": 30, "resume website": 40, "cv website": 40,
        "موقع شخصي": 40, "بورتفوليو": 50, "معرض أعمال": 40, "صفحة هبوط": 30,
    },
}

HARD_REJECT_KEYWORDS = {
    "graphic design", "logo", "photoshop", "illustrator", "video editing",
    "motion graphics", "translation", "seo", "digital marketing",
}

SOFT_NEGATIVE_KEYWORDS = {
    "wordpress": -40, "woocommerce": -40, "shopify": -40, "laravel": -40,
    "django": -30, "flask": -20, "react": -35, "reactjs": -35, "angular": -35,
    "vue": -35, "next.js": -35, "node": -30, "nodejs": -30, "express": -25,
    "backend": -30, "frontend": -30, "full stack": -40, "authentication": -30,
    "authorization": -25, "login": -15, "admin panel": -25, "control panel": -20,
    "crud": -20, "rest api": -25, "api": -15, "microservice": -30,
    "microservices": -30, "erp": -50, "crm": -50, "wms": -50, "warehouse": -40,
    "warehouse management": -50, "inventory": -30, "inventory management": -40,
    "supplier": -20, "customer management": -20, "flutter": -40,
    "react native": -40, "android": -40, "ios": -40,
}

# ---- internal working state: everything as {category_name: {keyword: score_or_None}} ----
DATA = {name: dict(kws) for name, kws in INTEREST_CATEGORIES.items()}
DATA["HARD_REJECT_KEYWORDS"] = {kw: None for kw in HARD_REJECT_KEYWORDS}
DATA["SOFT_NEGATIVE_KEYWORDS"] = dict(SOFT_NEGATIVE_KEYWORDS)

CATEGORY_ORDER = list(DATA.keys())


class KeywordEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Keyword Category Drag & Drop Editor")
        self.geometry("1400x800")

        self.listboxes = {}  # category -> Listbox
        self.drag_data = {"item_text": None, "source": None}

        container = tk.Frame(self)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container)
        scrollbar = tk.Scrollbar(container, orient="horizontal", command=canvas.xview)
        self.grid_frame = tk.Frame(canvas)

        self.grid_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        canvas.configure(xscrollcommand=scrollbar.set)

        canvas.pack(side="top", fill="both", expand=True)
        scrollbar.pack(side="bottom", fill="x")

        for col, category in enumerate(CATEGORY_ORDER):
            self._build_column(self.grid_frame, category, col)

        export_btn = tk.Button(self, text="Export keywords.py", command=self.export,
                                bg="#2d6cdf", fg="white", font=("Segoe UI", 11, "bold"))
        export_btn.pack(pady=8)

    def _build_column(self, parent, category, col):
        frame = tk.Frame(parent, bd=1, relief="solid")
        frame.grid(row=0, column=col, padx=4, pady=4, sticky="n")

        tk.Label(frame, text=category, font=("Segoe UI", 10, "bold")).pack(pady=(4, 2))

        lb = tk.Listbox(frame, width=28, height=28, selectmode="browse",
                         exportselection=False)
        lb.pack(padx=4, pady=2)
        self.listboxes[category] = lb
        self._refresh_listbox(category)

        lb.bind("<Button-1>", self._on_press)
        lb.bind("<B1-Motion>", self._on_motion)
        lb.bind("<ButtonRelease-1>", self._on_release)
        lb.bind("<Double-Button-1>", self._on_double_click)

        add_btn = tk.Button(frame, text="+ add", command=lambda c=category: self._add_keyword(c))
        add_btn.pack(pady=(2, 6))

    def _refresh_listbox(self, category):
        lb = self.listboxes[category]
        lb.delete(0, "end")
        for kw, score in sorted(DATA[category].items()):
            label = kw if score is None else f"{kw}  ({score})"
            lb.insert("end", label)

    def _keyword_at(self, category, index):
        items = sorted(DATA[category].items())
        if 0 <= index < len(items):
            return items[index][0]
        return None

    # ---- drag and drop handlers ----
    def _on_press(self, event):
        lb = event.widget
        index = lb.nearest(event.y)
        category = self._category_for_listbox(lb)
        kw = self._keyword_at(category, index)
        if kw is not None:
            self.drag_data["item_text"] = kw
            self.drag_data["source"] = category

    def _on_motion(self, event):
        pass  # visual feedback not required for a plain version

    def _on_release(self, event):
        if not self.drag_data["item_text"]:
            return
        widget_under = self.winfo_containing(event.x_root, event.y_root)
        target_category = self._category_for_listbox(widget_under)

        source = self.drag_data["source"]
        kw = self.drag_data["item_text"]

        if target_category and target_category != source:
            score = DATA[source].pop(kw)
            # if moving into a category that uses scores but item had none, ask for one
            if score is None and target_category != "HARD_REJECT_KEYWORDS":
                new_score = simpledialog.askinteger(
                    "Score", f"Score for '{kw}' in {target_category}:", initialvalue=20)
                score = new_score if new_score is not None else 20
            if target_category == "HARD_REJECT_KEYWORDS":
                score = None
            DATA[target_category][kw] = score
            self._refresh_listbox(source)
            self._refresh_listbox(target_category)

        self.drag_data["item_text"] = None
        self.drag_data["source"] = None

    def _category_for_listbox(self, widget):
        for category, lb in self.listboxes.items():
            if widget is lb:
                return category
        return None

    def _on_double_click(self, event):
        lb = event.widget
        index = lb.nearest(event.y)
        category = self._category_for_listbox(lb)
        kw = self._keyword_at(category, index)
        if kw is None:
            return

        if category == "HARD_REJECT_KEYWORDS":
            if messagebox.askyesno("Delete", f"Remove '{kw}' from HARD_REJECT_KEYWORDS?"):
                del DATA[category][kw]
                self._refresh_listbox(category)
            return

        current = DATA[category][kw]
        new_score = simpledialog.askinteger(
            "Edit score", f"New score for '{kw}' (blank/cancel to delete):",
            initialvalue=current)
        if new_score is None:
            if messagebox.askyesno("Delete", f"Remove '{kw}' from {category}?"):
                del DATA[category][kw]
        else:
            DATA[category][kw] = new_score
        self._refresh_listbox(category)

    def _add_keyword(self, category):
        kw = simpledialog.askstring("New keyword", f"Keyword to add to {category}:")
        if not kw:
            return
        kw = kw.strip()
        if not kw:
            return
        if category == "HARD_REJECT_KEYWORDS":
            DATA[category][kw] = None
        else:
            score = simpledialog.askinteger("Score", f"Score for '{kw}':", initialvalue=20)
            DATA[category][kw] = score if score is not None else 20
        self._refresh_listbox(category)

    # ---- export ----
    def export(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".py", initialfile="keywords.py",
            filetypes=[("Python file", "*.py")])
        if not path:
            return

        lines = ["INTEREST_CATEGORIES = {"]
        for category in CATEGORY_ORDER:
            if category in ("HARD_REJECT_KEYWORDS", "SOFT_NEGATIVE_KEYWORDS"):
                continue
            lines.append(f'    "{category}": {{')
            for kw, score in sorted(DATA[category].items()):
                lines.append(f'        {kw!r}: {score},')
            lines.append("    },\n")
        lines.append("}\n")

        lines.append("HARD_REJECT_KEYWORDS = {")
        for kw in sorted(DATA["HARD_REJECT_KEYWORDS"]):
            lines.append(f"    {kw!r},")
        lines.append("}\n")

        lines.append("SOFT_NEGATIVE_KEYWORDS = {")
        for kw, score in sorted(DATA["SOFT_NEGATIVE_KEYWORDS"].items()):
            lines.append(f"    {kw!r}: {score},")
        lines.append("}\n")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        messagebox.showinfo("Exported", f"Saved to {path}")


if __name__ == "__main__":
    app = KeywordEditor()
    app.mainloop()

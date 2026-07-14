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
    "crud": -20, "microservice": -30,
    "microservices": -30, "erp": -50, "crm": -50, "wms": -50, "warehouse": -40,
    "warehouse management": -50, "inventory": -30, "inventory management": -40,
    "supplier": -20, "customer management": -20, "flutter": -40,
    "react native": -40, "android": -40, "ios": -40,
}

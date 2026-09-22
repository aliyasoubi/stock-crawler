"""SQL-warehouse ETL: consumes stock_crawler.crawl's parsing/export contracts. One-way dependency (core <- crawl <- warehouse); crawl never imports warehouse."""

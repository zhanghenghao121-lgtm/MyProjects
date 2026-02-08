# AI学习：
## RAG
Embedding：向量化
python中的语义化模型：Sentence Transformers
相似度原理：余弦，欧里距离
向量数据库用了存储和查找。近似最近邻（ANN）用来搜索
### ChromaDB向量数据库的使用：
1、将需要存入数据库的文件进行切割，转为列表
2、进行向量化，embedding.encode()
3、存入数据库（需要使用到collection集合，collection = model
```
client = chromadb.PersistentClient(path="./chroma_db")#创建文件数据库
#创建集合
collection = client.get_or_create_collection(
    name="zhh_collection",
    metadata={
        "介绍":"这是一个文本文件数据库",
        "hnsw:space":"cosine"
    }
)

```
collecton.add() collection.query()）
问题：
> - 文件类型不能只支持txt
- 文件长度过大时需要进行切割
- RAG结果需要进行评估
### 需要大模型框架--LangChain
LangChain针对rag的部分：
Loaders（文件加载器）
Text Splitter（文本分割器）
Embedding Model（嵌入模型）
VectorStores（向量存储）
### 1、安装依赖
pip install langchain langchain-openai
langchain:框架本身
langchain-openai:集成openai的组件
langchain-chromadb:集成chromadb的组件
### 设置模型：llm大模型，embedding模型
文件分割：滑动窗口分块 chunk_overlap
text_splitter = RecursiveCharacterTextSplitter(chunk_size = 500,chunk_overlap = 100)#迭代切片
向量存储：vector_store = Chroma(embedding_function = embedding_model,persist_directory="./chroma_v3")
检索器：retriever = vector_store.as_retriever(search_kwargs = {"k":5})

使用提示词模板 langchain.template，通用提示词模板
FewShot提示词模版
format方法：返回字符串，替换字符串
invoke方法：返回promptValue对象，解析占位符生成提示词invoke({"k":v,"k":v}...)
### 编排“链”
用管道符号进行连接

加载文档
切分文档
存储文档
## RAG优化方向
分块策略
混合检索
重排序
查询转换
微调（embedding模型，LLM模型）
多模态RAG
Graph RAG（知识图谱）
Agent RAG（智能体）


```
project-root/
├─ public/                # 静态资源（不经过打包）
│  ├─ images/             # 图片资源
│  │  └─ logo.png
│  └─ favicon.ico
├─ src/
│  ├─ assets/             # 项目资源（可被打包）
│  │  ├─ css/
│  │  │  └─ custom.css    # 自定义样式
│  │  ├─ js/
│  │  │  └─ custom.js     # 自定义 JS
│  │  └─ images/
│  │     └─ bg.jpg
│  ├─ api/
│  │  └─ http.js          # Axios 或封装请求
│  ├─ views/
│  │  └─ Login.vue
│  ├─ stores/
│  │  └─ user.js
│  ├─ App.vue
│  ├─ main.js
│  └─ router.js
├─ package.json
└─ vite.config.js
```
爬取动漫资源网站：https://acg.rip/
https://acg.rip/?term=输入的搜索关键词

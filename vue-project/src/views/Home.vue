<template>
  <div class="home">
    <header class="topbar">
      <h1 class="title">首页</h1>
      <button class="logout" @click="logout">退出</button>
    </header>

    <section class="card">
      <h2 class="section-title">爬取动漫数据</h2>
      <div class="form">
        <label class="field">
          <span>搜索关键词</span>
          <input v-model.trim="keyword" type="text" placeholder="例如：高达 / 夏目 / EVA" />
        </label>
      </div>
      <button class="primary" :disabled="loading" @click="goAcg">
        <span v-if="loading" class="spinner" />
        爬取动漫数据
      </button>
      <p v-if="message" class="message">{{ message }}</p>
    </section>
  </div>
</template>

<script src="../js/home.js"></script>

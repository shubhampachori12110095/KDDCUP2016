"""
Created on Jul 7, 2014

@author: luamct
"""

import chardet
import numpy as np
import networkx as nx
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from indexer.indexer import Indexer
from mymysql import MyMySQL
from collections import defaultdict
from exceptions import TypeError

from pylucene import Index
import itertools
import os
import logging as log
import words
import config
import utils



# Database connection
db = MyMySQL(db=config.DB_NAME,
             user=config.DB_USER,
             passwd=config.DB_PASSWD)


########################################
## Helper methods
########################################

def sorted_tuple(a, b):
  """ Simple pair sorting to avoid repetitions when inserting into set or dict. """
  return (a, b) if a < b else (b, a)


def get_unicode(s):
  """ Return an unicode of the string encoded with the most likely format. """
  if not s:
    return unicode("", "utf-8")

  return unicode(s, chardet.detect(s)['encoding'])


def get_all_edges():
  """
  Retrieve all edges from the database.
  """
  return db.select(fields=["citing", "cited"], table="graph")


def show_stats(graph):
  print "%d nodes and %d edges." % (graph.number_of_nodes(), graph.number_of_edges())


def write_graph(graph, outfile):
  """
  Write the networkx graph into a file in the gexf format.
  """
  log.info("Dumping graph: %d nodes and %d edges." % (graph.number_of_nodes(), graph.number_of_edges()))
  nx.write_gexf(graph, outfile, encoding="utf-8")


def only_tokenized(doc_ids):
  """
  Return only the ids that are marked as tokenized in the DB (eligible for topic extraction).
  """
  allowed_ids = set(db.select("id", table="tasks", where="status='TOKENIZED' OR status='CONVERTED'"))
  return (doc_ids & allowed_ids)


def get_paper_year(paper_id):
  """
  Returns the year of the given paper as stored in the DB.
  """
  year = db.select_one(fields="year", table="papers", where="id='%s'" % paper_id)
  return (int(year) if year else 0)


def show(doc_id):
  """ Small utility method to show a document on the browser. """
  from subprocess import call, PIPE

  call(["google-chrome", "--incognito", "/data/pdf/%s.pdf" % doc_id], stdout=PIPE, stderr=PIPE)


def add_attributes(graph, entities, node_ids, atts):
  """
  Adds attributes to the nodes associated to the given entities (papers, authors, etc.)
  """
  for entity in entities:
    graph.add_node(node_ids[entity], **atts[entity])


def normalize_edges(edges):
  """
  Normalize the weight on given edges dividing by the maximum weight found.
  """
  wmax = 0.0
  for _u, _v, w in edges:
    wmax = max(w, wmax)

  return [(u, v, w / float(wmax)) for u, v, w in edges]


def similarity(d1, d2):
  """
  Cosine similarity between sparse vectors represented as dictionaries.
  """
  sim = 0.0
  for k in d1:
    if k in d2:
      sim += d1[k] * d2[k]

  dem = np.sqrt(np.square(d1.values()).sum()) * np.sqrt(np.square(d2.values()).sum())
  return sim / dem


def get_rules_by_lift(transactions, min_lift=1.0):
  """
  Get strong rules from transactions and minimum lift provided.
  """
  freqs1 = defaultdict(int)  # Frequencies of 1-itemsets
  freqs2 = defaultdict(int)  # Frequencies of 2-itemsets
  for trans in transactions:
    for i in trans:
      freqs1[i] += 1

    # If there are at least 2 items, let's compute pairs support
    if len(trans) >= 2:
      for i1, i2 in itertools.combinations(trans, 2):
        freqs2[sorted_tuple(i1, i2)] += 1

  n = float(len(transactions))

  # Check every co-occurring ngram
  rules = []
  for (i1, i2), f in freqs2.items():

    # Consider only the ones that appear more than once together,
    # otherwise lift values can be huge and not really significant
    if f >= 1:
      lift = f * n / (freqs1[i1] * freqs1[i2])

      # Include only values higher than min_lift
      if lift >= min_lift:
        rules.append((i1, i2, lift))

  return rules


########################################
## Class definitions
########################################


class GraphBuilder:
  """
  Graph structure designed to store edges and operate efficiently on some specific
  graph building and expanding operations.
  """

  def __init__(self, edges):

    self.citing = defaultdict(list)
    self.cited = defaultdict(list)

    for f, t in edges:
      self.citing[str(f)].append(str(t))
      self.cited[str(t)].append(str(f))


  def follow_nodes(self, nodes):
    """
    Return all nodes one edge away from the given nodes.
    """
    new_nodes = set()
    for n in nodes:
      new_nodes.update(self.citing[n])
      new_nodes.update(self.cited[n])

    return new_nodes


  def subgraph(self, nodes):
    """
    Return all edges between the given nodes.
    """
    # Make sure lookup is efficient
    nodes = set(nodes)

    new_edges = []
    for n in nodes:

      for cited in self.citing[n]:
        if (n != cited) and (cited in nodes):
          new_edges.append((n, cited))

      for citing in self.cited[n]:
        if (n != citing) and (citing in nodes):
          new_edges.append((citing, n))

    return set(new_edges)


class ModelBuilder:
  """
  Main class for building the graphical model. The layers are built separately in their
  corresponding methods. Every layer is cached in a folder defined by the main parameters.
  """

  def __init__(self, include_attributes=False):
    """
    Initializes structures and load data into memory, such as the text index and
    the citation graph.
    """
    # Build text index if non-existing
    if not os.path.exists(config.INDEX_PATH):
      indexer = Indexer()
      indexer.add_papers(config.INDEX_PATH, include_text=False)

    # Load text index
    self.index = Index(config.INDEX_PATH, similarity="tfidf")

    # Graph structure that allows fast access to nodes and edges
    self.edges_lookup = GraphBuilder(get_all_edges())

    # If attributes should be fetched and included in the model for each type of node.
    # Should be true for visualization and false for pure relevance calculation.
    self.include_attributes = include_attributes

    # Pre-load the year and venue of each publication for faster access later
    self.pub_years = {}
    self.pub_venues = {}
    rows = db.select(fields=["id", "year", "venue_id"], table="papers")
    for pub, year, venue in rows:
      self.pub_years[str(pub)] = int(year or 0)
      if venue:
        self.pub_venues[pub] = venue

    # Create a helper boolean to check if citation contexts are
    # going to be used (some datasets don't have it available)
    self.use_contexts = (config.DATASET == 'csx')

    # Load vocabulary for the tokens in the citation contexts
    # if self.use_contexts:
    #   self.ctxs_vocab, self.nctx = words.read_vocab(config.CTXS_VOCAB_PATH)

    log.debug("ModelBuilder constructed.")


  def query_tfidf(self, query):
    return words.get_tfidf(query, self.ctxs_vocab, self.nctx)


  def get_context_based_weights_file(self, query, nodes, edges):
    """
    Get edge weights according to textual similarity between
    the query and the citation context.
    """
    # If the dataset doesn't not support citation contexts,
    # just use weight=1
    if not self.use_contexts:
      return [(u, v, 1.0) for (u, v) in edges]

    # Load contexts around citations for the provided edges
    ctxs = self.get_edge_contexts(nodes, edges)

    # Get TF-IDF representation for the query
    query_vec = self.query_tfidf(query)

    # Weights the edges according to the similarity to contexts' similarity to the query
    weighted_edges = []
    self.ctx_query_sims = []
    for u, v in edges:
      if (u, v) in ctxs:
        ctx_query_sim = similarity(query_vec, ctxs[(u, v)])
      else:
        ctx_query_sim = 0.0

      weighted_edges.append((u, v, ctx_query_sim))

    return weighted_edges


  def get_context_based_weights(self, query, nodes, edges):
    """
    Get edge weights according to textual similarity between
    the query and the citation context.
    """
    # If the dataset doesn't not support citation contexts,
    # just use weight=1
    if not self.use_contexts:
      return [(u, v, 1.0) for (u, v) in edges]

    ctxs = []
    for citing, cited in edges :
      ctx = db.select_one(fields="context",
                          table="graph",
                          where="citing='%s' AND cited='%s'" % (citing, cited))

      if ctx == None: ctx = u''
      # Remove placeholders marked with =-= and -=-
      beg_idx = ctx.find("=-=")
      end_idx = ctx.find("-=-", beg_idx) + 3

      ctx = ctx[:beg_idx] + ctx[end_idx:]
      ctxs.append(ctx)

    # Get the TF_IDF vector representation for the contexts
    vec = TfidfVectorizer(min_df=2, max_df=0.5, stop_words="english", ngram_range=(1, 3))
    vctxs = vec.fit_transform(ctxs)

    # Get TF-IDF vector representation for the query (given and returned as a vector)
    vquery = vec.transform([query])[0]

    # Weights the edges according to the similarity to contexts' similarity to the query
    weighted_edges = [(citing, cited , cosine_similarity(vquery, vctxs[i])[0][0])
                      for i, (citing, cited) in enumerate(edges)]

    # print "-- %s --" % query
    # for i, ctx in enumerate(ctxs):
    #   print
    #   print ctx
    #   print weighted_edges[i][2]

    return weighted_edges


  def get_pubs_layer(self, query, n_starting_nodes, n_hops, exclude_list=set()):
    """
    First the top 'n_starting_nodes' documents are retrieved using an index
    and ranked using standard TF-IDF . Then we follow n_hops from these nodes
    to have the first layer of the graph (papers).
    """
    # Must be called on every thread accessing the index
    self.index.attach_thread()

    # Fetches all document that have at least one of the terms
    docs = self.index.search(query,
                             search_fields=["title", "abstract"],
                             return_fields=["id"],
                             ignore=exclude_list,
                             limit=n_starting_nodes)

    # Store normalized query similarities for each matched document in a class attribute.
    # Non matched paper will get a 0.0 value given by the defaultdict.
    #		self.query_sims = defaultdict(int)
    #		max_query_sim = max(scores)
    #		for i in xrange(len(docs)) :
    #			self.query_sims[docs[i]['id']] = scores[i]/max_query_sim

    # Add the top n_starting_nodes as long as not in exclude list
    #		i = 0
    #		doc_ids = []
    #		while (len(doc_ids) < n_starting_nodes) :
    #			if (docs[i]['id'] not in exclude_list) :
    #				doc_ids.append(docs[i]['id'])
    #			i += 1
    #		doc_ids = [doc['id'] for doc in docs[:n_starting_nodes]]

    #		if len(exclude_list)==0 :
    #			raise Exception("No pubs in the exclude list.")

    #		most_similar = docs[0][0]
    #		pub_id = list(exclude_list)[0]

    #		c1 = utils.get_cited(db, pub_id)
    #		c2 = utils.get_cited(db, most_similar)
    #		print query
    #		print utils.get_title(db, most_similar)
    #		print len(c1), len(c2), len(set(c1)&set(c2))

    # Get doc ids as uni-dimensional list
    nodes = set([str(doc[0]) for doc in docs])
    new_nodes = nodes

    # We hop h times including all the nodes from these hops
    for h in xrange(n_hops):
      new_nodes = self.edges_lookup.follow_nodes(new_nodes)

      # Remove documents from the exclude list and keep only processed ids
      new_nodes -= exclude_list
      #			new_nodes &= self.allowed_ids

      # Than add them to the current set
      nodes.update(new_nodes)

      log.debug("Hop %d: %d nodes." % (h + 1, len(nodes)))

    # Get the query similarities from the index. They'll be used latter when
    # assembling the layers into a NetworkX graph
    self.query_scores = self.index.get_query_scores(query, fields=["title", "abstract"], doc_ids=nodes)

    # Get the edges between the given nodes and add a constant the weight for each
    edges = self.edges_lookup.subgraph(nodes)

    # Get edge weights according to textual similarity between
    # the query and the citation context
    weighted_edges = self.get_context_based_weights(query, nodes, edges)

    # To list to preserve element order
    nodes = list(nodes)

    # Save into cache for reusing
    # 		cPickle.dump((nodes, edges, self.query_sims), open(cache_file, 'w'))

    return nodes, weighted_edges


  def get_authors(self, doc_id):
    """
    Return the authors associated with the given paper, if available.
    """
    #		return db.select("cluster", table="authors_clean", where="paperid='%s'" % doc_id)
    return db.select("author_id", table="authorships", where="paper_id='%s'" % doc_id)


  def get_cached_coauthorship_edges(self, authors):
    """
    Return all the collaboration edges between the given authors. Edges to authors not provided are
    not included.
    """
    # For efficient lookup
    authors = set(authors)

    edges = set()
    for author_id in authors:
      coauthors = db.select(["author1", "author2", "npapers"], "coauthorships",
                  where="author1=%d OR author2=%d" % (author_id, author_id))
      for a1, a2, npapers in coauthors:

        # Apply log transformation to smooth values and avoid outliers
        # crushing other values after normalization
        npapers = 1.0 + np.log(npapers)

        if (a1 in authors) and (a2 in authors):
          edge = (a1, a2, 1.0) if a1 < a2 else (a2, a1, 1.0)
          edges.add(edge)

    # Normalize by max value and return them as a list
    return normalize_edges(edges)


  def get_coauthorship_edges(self, authors):
    """
    Return all the collaboration edges between the given authors. Edges to authors not provided are
    not included.
    """
    # For efficient lookup
    authors = set(authors)

    edges = set()
    for author_id in authors:
      coauthorships = db.select_query("""SELECT b.author_id FROM authorships a, authorships b
                                         WHERE (a.author_id=%d) AND (b.author_id!=%d) AND a.paper_id=b.paper_id""" \
                      % (author_id, author_id))

      # Count coauthorshiped pubs
      coauthors = defaultdict(int)
      for (coauthor,) in coauthorships:
        if coauthor in authors:
          coauthors[(author_id, coauthor)] += 1

      for (a1, a2), npapers in coauthors.items():

        # Apply log transformation to smooth values and avoid outliers
        # crushing other values after normalization
        weight = 1.0 + np.log(npapers)

        if (a1 in authors) and (a2 in authors):
          edge = (a1, a2, weight) if a1 < a2 else (a2, a1, weight)
          edges.add(edge)

    # Normalize by max value and return them as a list
    return normalize_edges(edges)


  def get_authorship_edges(self, papers_authors):
    """
    Return authorship edges [(doc_id, author), ...]
    """
    edges = []
    for doc_id, authors in papers_authors.items():
      edges.extend([(doc_id, author, 1.0) for author in authors])

    return edges


  def get_authors_layer(self, papers, ign_cache=False):
    """
    Retrieve relevant authors from DB (author of at least one paper given as argument)
    and assemble co-authorship and authorship nodes and edges.
    """

    # Try to load from cache
    # 		cache_file = "%s/authors.p" % self.cache_folder
    # 		if (not ign_cache) and os.path.exists(cache_file) :
    # 			return cPickle.load(open(cache_file, 'r'))

    all_authors = set()
    papers_authors = {}
    for paperid in papers:
      paper_authors = self.get_authors(paperid)

      papers_authors[paperid] = paper_authors
      all_authors.update(paper_authors)


    #		coauth_edges = self.get_coauthorship_edges(all_authors)
    coauth_edges = self.get_cached_coauthorship_edges(all_authors)
    auth_edges = self.get_authorship_edges(papers_authors)
    all_authors = list(all_authors)

    # Save into cache for reuse
    # 		cPickle.dump((all_authors, coauth_edges, auth_edges), open(cache_file, 'w'))

    return all_authors, coauth_edges, auth_edges


  def get_relevant_topics(self, doc_topics, ntop=None, above=None):
    """
    Get the most important topics for the given document by either:
      * Taking the 'ntop' values if 'ntop' id provided or
      * Taking all topics with contributions greater than 'above'.
    """
    if ntop:
      return np.argsort(doc_topics)[::-1][:ntop]

    if above:
      return np.where(doc_topics > above)[0]

    raise TypeError("Arguments 'ntop' and 'above' cannot be both None.")


  def get_frequent_topic_pairs(self, topics_per_document, min_interest):

    freqs1 = defaultdict(int)  # Frequencies of 1-itemsets
    freqs2 = defaultdict(int)  # Frequencies of 2-itemsets
    for topics in topics_per_document:
      for t in topics:
        freqs1[t] += 1

      if len(topics) >= 2:
        for t1, t2 in itertools.combinations(topics, 2):
          freqs2[sorted_tuple(t1, t2)] += 1

    total = float(len(topics_per_document))

    rules = []
    for (t1, t2), v in sorted(freqs2.items(), reverse=True, key=lambda (k, v): v):

      int12 = float(v) / freqs1[t1] - freqs1[t2] / total
      int21 = float(v) / freqs1[t2] - freqs1[t1] / total

      if int12 >= min_interest: rules.append((t1, t2, int12))
      if int21 >= min_interest: rules.append((t2, t1, int21))

    # 	for interest, (t1, t2) in sorted(rules, reverse=True) :
    # 		print "(%d -> %d) :\t%f" % (t1, t2, interest) - freqs1[t2]/total
    # 		print "(%d -> %d) :\t%f" % (t2, t1, interest) - freqs1[t1]/total

    return rules


  def get_topics_layer_from_db(self, doc_ids, min_conf_topics):
    """
    Run topic modeling for the content on the given papers and assemble the topic nodes
    and edges.
    """
    # 		topics, doc_topics, tokens = topic_modeling.get_topics_online(doc_ids, ntopics=200, beta=0.1,
    # 																																cache_folder=self.cache_folder, ign_cache=False)

    # Build topic nodes and paper-topic edges
    topic_nodes = set()
    topic_paper_edges = set()

    # Retrieve top topics for each document from the db
    topic_ids_per_doc = []
    for doc_id in doc_ids:

      topics = db.select(fields=["topic_id", "value"], table="doc_topics", where="paper_id='%s'" % doc_id)
      if len(topics):
        topic_ids, topic_values = zip(*topics)

        topic_ids_per_doc.append(topic_ids)
        # 				topic_values_per_doc.append(topic_values)

        topic_nodes.update(topic_ids)
        topic_paper_edges.update([(doc_id, topic_ids[t], topic_values[t]) for t in xrange(len(topic_ids))])

      # 		for d in xrange(len(doc_ids)) :
      # 			topic_ids = topic_ids_per_doc[d]
      # 			topic_values = topic_values_per_doc[d]


    # Normalize edge weights with the maximum value
    topic_paper_edges = normalize_edges(topic_paper_edges)

    # From the list of relevant topics f
    #		rules = self.get_frequent_topic_pairs(topic_ids_per_doc, min_conf_topics)
    topic_topic_edges = get_rules_by_lift(topic_ids_per_doc, min_conf_topics)
    topic_topic_edges = normalize_edges(topic_topic_edges)

    # Get the density of the ngram layer to feel the effect of 'min_topics_lift'
    self.topic_density = float(len(topic_topic_edges)) / len(topic_nodes)

    #		get_name = lambda u: db.select_one(fields="words", table="topic_words", where="topic_id=%d"%u)
    #		top = sorted(topic_topic_edges, key=lambda t:t[2], reverse=True)
    #		for u, v, w in top :
    #			uname = get_name(u)
    #			vname = get_name(v)
    #			print "%s\n%s\n%.3f\n" % (uname, vname, w)

    # Cast topic_nodes to list so we can assure element order
    topic_nodes = list(topic_nodes)

    return topic_nodes, topic_topic_edges, topic_paper_edges


  #	def get_topics_layer(self, doc_ids, min_conf_topics) :
  #		'''
  #		Run topic modeling for the content on the given papers and assemble the topic nodes
  #		and edges.
  #		'''
  #		topics, doc_topics, tokens = topic_modeling.get_topics_online(self.cache_folder, ntopics=200,
  #																																beta=0.1, ign_cache=False)
  #
  #		doc_topic_above = DOC_TOPIC_THRES
  #
  #		topic_nodes = set()
  #		topic_paper_edges = set()
  #		topics_per_document = []
  #		for d in xrange(len(doc_ids)) :
  #			relevant_topics = self.get_relevant_topics(doc_topics[d], above=doc_topic_above)
  #
  #			# This data structure is needed for the correlation between topics
  #			topics_per_document.append(relevant_topics)
  #
  #			topic_nodes.update(relevant_topics)
  #			topic_paper_edges.update([(doc_ids[d], t, doc_topics[d][t]) for t in relevant_topics])
  #
  #		# Normalize edge weights with the maximum value
  #		topic_paper_edges = normalize_edges(topic_paper_edges)
  #
  #		# From the list of relevant topics f
  #		rules = self.get_frequent_topic_pairs(topics_per_document)
  #
  #		# Add only edges above certain confidence. These edge don't
  #		# need to be normalized since 0 < confidence < 1.
  #		topic_topic_edges = set()
  #		for interest, (t1, t2) in rules :
  #			if interest >= min_conf_topics :
  #				topic_topic_edges.add( (t1, t2, interest) )
  #
  #		# Cast topic_nodes to list so we can assure element order
  #		topic_nodes = list(topic_nodes)
  #
  #		# Select only the names of the topics being considered here
  #		# and store in a class attribute
  #		topic_names = topic_modeling.get_topic_names(topics, tokens)
  #		self.topic_names = {tid: topic_names[tid] for tid in topic_nodes}
  #
  #		return topic_nodes, topic_topic_edges, topic_paper_edges, tokens


  #	def get_words_layer_from_db(self, doc_ids):
  #		'''
  #		Create words layers by retrieving TF-IDF values from the DB (previously calculated).
  #		'''
  #
  #		word_nodes = set()
  #		paper_word_edges = set()
  #
  #		for doc_id in doc_ids :
  #			rows = db.select(fields=["word", "value"],
  #											 table="doc_words",
  #											 where="paper_id='%s'"%doc_id,
  #											 order_by=("value","desc"),
  #											 limit=5)
  #			top_words, top_values = zip(*rows)
  #
  #			word_nodes.update(top_words)
  #			paper_word_edges.update([(doc_id, top_words[t], top_values[t]) for t in range(len(top_words))])
  #
  #		# Normalize edges weights by their biggest value
  #		paper_word_edges = normalize_edges(paper_word_edges)
  #
  #		return word_nodes, paper_word_edges


  #	def get_ngrams_layer_from_db2(self, doc_ids):
  #		'''
  #		Create words layers by retrieving TF-IDF values from the DB (previously calculated).
  #		'''
  #		word_nodes = set()
  #		paper_word_edges = set()
  #
  #		ngrams_per_doc = []
  #		for doc_id in doc_ids :
  #			rows = db.select(fields=["ngram", "value"],
  #											 table="doc_ngrams",
  #											 where="(paper_id='%s') AND (value>=%f)" % (doc_id, config.MIN_NGRAM_TFIDF))
  #
  #
  #			if (len(rows) > 0) :
  #				top_words, top_values = zip(*rows)
  #
  #				word_nodes.update(top_words)
  #				paper_word_edges.update([(doc_id, top_words[t], top_values[t]) for t in range(len(top_words))])
  #
  #				ngrams_per_doc.append(top_words)
  #
  #		## TEMPORARY ##
  #		# PRINT MEAN NGRAMS PER DOC
  ##		mean_ngrams = np.mean([len(ngrams) for ngrams in ngrams_per_doc])
  ##		print "%f\t" % mean_ngrams,
  #
  #		# Get get_rules_by_lift between co-occurring ngrams to create edges between ngrams
  #		word_word_edges = get_rules_by_lift(ngrams_per_doc, min_lift=config.MIN_NGRAM_LIFT)
  #
  ##		print len(word_nodes), "word nodes."
  ##		print len(word_word_edges), "word-word edges."
  ##		for e in word_word_edges :
  ##			print e
  #
  ##		for rule in sorted(rules, reverse=True) :
  ##			print rule
  #
  #		# Normalize edges weights by their biggest value
  #		word_word_edges = normalize_edges(word_word_edges)
  #		paper_word_edges = normalize_edges(paper_word_edges)
  #
  #		return word_nodes, word_word_edges, paper_word_edges


  def get_ngrams_layer_from_db(self, doc_ids, min_ngram_lift):
    """
    Create words layers by retrieving TF-IDF values from the DB (previously calculated).
    """
    word_nodes = set()
    paper_word_edges = list()

    doc_ids_str = ",".join(["'%s'" % doc_id for doc_id in doc_ids])

    MIN_NGRAM_TFIDF = 0.25

    table = "doc_ngrams"
    rows = db.select(fields=["paper_id", "ngram", "value"], table=table,
             where="paper_id IN (%s) AND (value>=%f)" % (doc_ids_str, MIN_NGRAM_TFIDF))

    #
    ngrams_per_doc = defaultdict(list)
    for doc_id, ngram, value in rows:
      word_nodes.add(ngram)
      paper_word_edges.append((str(doc_id), ngram, value))

      ngrams_per_doc[str(doc_id)].append(ngram)

    # Get get_rules_by_lift between co-occurring ngrams to create edges between ngrams
    word_word_edges = get_rules_by_lift(ngrams_per_doc.values(), min_lift=min_ngram_lift)

    # Get the density of the ngram layer to feel the effect of 'min_ngram_lift'
    self.ngram_density = float(len(word_word_edges)) / len(word_nodes)
    self.nwords = len(word_nodes)

    # Normalize edges weights by their biggest value
    word_word_edges = normalize_edges(word_word_edges)
    paper_word_edges = normalize_edges(paper_word_edges)

    return word_nodes, word_word_edges, paper_word_edges


  def get_keywords_layer_from_db(self, doc_ids, min_ngram_lift):
    """
    Create words layers by retrieving TF-IDF values from the DB (previously calculated).
    """
    word_nodes = set()
    paper_word_edges = list()

    doc_ids_str = ",".join(["'%s'" % doc_id for doc_id in doc_ids])

    where = "paper_id IN (%s)" % doc_ids_str
    if config.KEYWORDS == "extracted":
      where += " AND (extracted=1)"

    elif config.KEYWORDS == "extended":
      where += " AND (extracted=0) AND (value>=%f)" % config.MIN_NGRAM_TFIDF

    elif config.KEYWORDS == "both":
      where += " AND (value>=%f)" % config.MIN_NGRAM_TFIDF

    rows = db.select(fields=["paper_id", "ngram"],
             table="doc_kws",
             where=where)

    #
    ngrams_per_doc = defaultdict(list)
    for doc_id, ngram in rows:
      word_nodes.add(ngram)
      paper_word_edges.append((str(doc_id), ngram, 1.0))

      ngrams_per_doc[str(doc_id)].append(ngram)

    # Get get_rules_by_lift between co-occurring ngrams to create edges between ngrams
    word_word_edges = get_rules_by_lift(ngrams_per_doc.values(), min_lift=min_ngram_lift)

    # Get the density of the ngram layer to feel the effect of 'min_ngram_lift'
    self.ngram_density = float(len(word_word_edges)) / len(word_nodes)
    self.nwords = len(word_nodes)

    # Normalize edges weights by their biggest value
    word_word_edges = normalize_edges(word_word_edges)
    paper_word_edges = normalize_edges(paper_word_edges)

    return word_nodes, word_word_edges, paper_word_edges


  def get_papers_atts(self, papers):
    """
    Fetch attributes for each paper from the DB.
    """
    atts = {}
    for paper in papers:
      title, venue = db.select_one(["title", "venue"], table="papers", where="id='%s'" % paper)
      title = title if title else ""
      venue = venue if venue else ""
      query_score = self.query_sims[paper] if (paper in self.query_sims) else 0.0
      atts[paper] = {"label": title, "title": title, "venue": venue, "query_score": query_score}

    return atts


  def get_authors_atts(self, authors):
    """
    Fetch attributes for each author from the DB.
    """
    atts = {}
    for author in authors:
      name, email, affil = db.select_one(["name", "email", "affil"], table="authors", where="cluster=%d" % author)
      npapers = str(db.select_one("count(*)", table="authors", where="cluster=%d" % author))
      name = name if name else ""
      email = email if email else ""
      affil = affil if affil else ""

      atts[author] = {"label": name, "name": name, "email": email, "affil": affil, "npapers": npapers}

    return atts


  def get_topics_atts(self, topics):
    """
    Fetch attributes for each topic.
    """
    topic_names = db.select(fields="words", table="topic_words", order_by="topic_id")
    atts = {}
    for topic in topics:
      topic_name = topic_names[topic]
      atts[topic] = {"label": topic_name, "description": topic_name}

    return atts


  def get_words_atts(self, words):
    """
    Fetch attributes for each word.
    """
    atts = {}
    for word in words:
      atts[word] = {"label": word}

    return atts


  def assemble_layers(self, pubs, citation_edges,
            authors, coauth_edges, auth_edges,
            topics, topic_topic_edges, paper_topic_edges,
            ngrams, ngram_ngram_edges, paper_ngram_edges,
            venues, pub_venue_edges):
    """
    Assembles the layers as an unified graph. Each node as an unique id, its type (paper,
    author, etc.) and a readable label (paper title, author name, etc.)
    """
    graph = nx.DiGraph()

    # These map the original identifiers for each type (paper doi, author id,
    # etc.) to the new unique nodes id.
    pubs_ids = {}
    authors_ids = {}
    topics_ids = {}
    words_ids = {}
    venues_ids = {}

    # Controls the unique incremental id generation
    next_id = 0

    # Add each paper providing an unique node id. Some attributes must be added
    # even if include_attributes is True, since they are used in ranking algorithm.
    for pub in pubs:
      pub = str(pub)

      #			if hasattr(self, 'query_sims') :
      #				query_score = float(self.query_sims[paper])  #if paper in self.query_sims else 0.0
      #			else :
      #				query_score = 0.0

      graph.add_node(next_id,
               type="paper",
               entity_id=pub,
               year=self.pub_years[pub],
               query_score=self.query_scores[pub])

      pubs_ids[pub] = next_id
      next_id += 1

    # Add citation edges (directed)
    for paper1, paper2, weight in citation_edges:
      graph.add_edge(pubs_ids[paper1], pubs_ids[paper2], weight=weight)


    # Add each author providing an unique node id
    for author in authors:
      graph.add_node(next_id, type="author", entity_id=author)

      authors_ids[author] = next_id
      next_id += 1


    # Add co-authorship edges on both directions (undirected)
    for author1, author2, weight in coauth_edges:
      graph.add_edge(authors_ids[author1], authors_ids[author2], weight=weight)
      graph.add_edge(authors_ids[author2], authors_ids[author1], weight=weight)

    # Add authorship edges on both directions (undirected)
    for paper, author, weight in auth_edges:
      graph.add_edge(pubs_ids[paper], authors_ids[author], weight=weight)
      graph.add_edge(authors_ids[author], pubs_ids[paper], weight=weight)


    ####################################

    #		# Add topic nodes
    #		for topic in topics :
    #			graph.add_node(next_id, type="topic", entity_id=topic)
    #
    #			topics_ids[topic] = next_id
    #			next_id += 1
    #
    #		# Add topic correlation edges (directed)
    #		for topic1, topic2, weight in topic_topic_edges :
    #			graph.add_edge(topics_ids[topic1], topics_ids[topic2], weight=weight)
    #			graph.add_edge(topics_ids[topic2], topics_ids[topic1], weight=weight)
    #
    #		# Add paper-topic edges (directed)
    #		for paper, topic, weight in paper_topic_edges :
    #			graph.add_edge(pubs_ids[paper], topics_ids[topic], weight=weight)
    #			graph.add_edge(topics_ids[topic], pubs_ids[paper], weight=weight)

    ####################################
    # Add ngram nodes
    for ngram in ngrams:
      graph.add_node(next_id, type="ngram", entity_id=ngram)

      words_ids[ngram] = next_id
      next_id += 1

    #		 Add word-word edges (undirected)
    for w1, w2, weight in ngram_ngram_edges:
      graph.add_edge(words_ids[w1], words_ids[w2], weight=weight)
      graph.add_edge(words_ids[w2], words_ids[w1], weight=weight)

    # Add paper-word edges (undirected)
    for paper, word, weight in paper_ngram_edges:
      graph.add_edge(pubs_ids[paper], words_ids[word], weight=weight)
      graph.add_edge(words_ids[word], pubs_ids[paper], weight=weight)

    ####################################
    # Add venues to the graph
    for venue in venues:
      graph.add_node(next_id, type="venue", entity_id=venue)

      venues_ids[venue] = next_id
      next_id += 1

    for pub, venue, weight in pub_venue_edges:
      graph.add_edge(pubs_ids[pub], venues_ids[venue], weight=weight)
      graph.add_edge(venues_ids[venue], pubs_ids[pub], weight=weight)


    # Get the attributes for each author
    # Get attributes for each paper
    if self.include_attributes:
      add_attributes(graph, pubs, pubs_ids, self.get_papers_atts(pubs))
      add_attributes(graph, authors, authors_ids, self.get_authors_atts(authors))
      add_attributes(graph, topics, topics_ids, self.get_topics_atts(topics))
      add_attributes(graph, words, words_ids, self.get_words_atts(words))

    return graph


  def parse_tfidf_line(self, line):
    parts = line.strip().split()
    tokens = parts[0::2]
    tfidf = map(float, parts[1::2])
    return dict(zip(tokens, tfidf))


  def get_edge_contexts(self, papers, citation_edges):

    citation_edges = set(citation_edges)

    tokens_per_citation = {}
    for citing in papers:
      if os.path.exists(config.CTX_PATH % citing):
        with open(config.CTX_PATH % citing, "r") as file:
          for line in file:
            cited, tokens_tfidf = line.strip().split('\t')

            if (citing, cited) in citation_edges:
              tokens_per_citation[(citing, cited)] = self.parse_tfidf_line(tokens_tfidf)

    return tokens_per_citation


  def get_venues_layer(self, pubs):
    """
    Returns the venues' ids and edges from publications to venues according
    to the venues used in the publications.
    """
    venues = set()
    pub_venue_edges = list()
    for pub in pubs:
      if pub in self.pub_venues:
        venue_id = self.pub_venues[pub]
        venues.add(venue_id)
        pub_venue_edges.append((pub, venue_id, 1.0))

    return list(venues), pub_venue_edges


  def build(self, query, n_starting_nodes, n_hops, min_topic_lift, min_ngram_lift, exclude=[]):
    """
    Build graph model from given query.
    """

    log.debug("Building model for query='%s', starting_nodes=%d and hops=%d." % (query, n_starting_nodes, n_hops))

    pubs, citation_edges = self.get_pubs_layer(query, n_starting_nodes, n_hops, set(exclude))
    log.debug("%d pubs and %d citation edges." % (len(pubs), len(citation_edges)))

    authors, coauth_edges, auth_edges = self.get_authors_layer(pubs)
    log.debug("%d authors, %d co-authorship edges and %d authorship edges." % (
      len(authors), len(coauth_edges), len(auth_edges)))

    #		topics, topic_topic_edges, pub_topic_edges = self.get_topics_layer_from_db(pubs, min_topic_lift)
    #		log.debug("%d topics, %d topic-topic edges and %d pub-topic edges."
    #										% (len(topics), len(topic_topic_edges), len(pub_topic_edges)))

    # Use the standard ngrams formulation if the config says so
    if config.KEYWORDS == "ngrams":
      words, word_word_edges, pub_word_edges = self.get_ngrams_layer_from_db(pubs, min_ngram_lift)

    # Otherwise use some variant of a keywords' layer
    else:
      words, word_word_edges, pub_word_edges = self.get_keywords_layer_from_db(pubs, min_ngram_lift)
    log.debug("%d words and %d pub-word edges." % (len(words), len(pub_word_edges)))

    venues, pub_venue_edges = self.get_venues_layer(pubs)
    log.debug("%d venues and %d pub-venue edges." % (len(venues), len(pub_venue_edges)))

    graph = self.assemble_layers(pubs, citation_edges,
                   authors, coauth_edges, auth_edges,
                   None, None, None,
                   #														topics, topic_topic_edges, pub_topic_edges,
                   words, word_word_edges, pub_word_edges,
                   venues, pub_venue_edges)

    # Writes the contexts of each edge into a file to be used efficiently
    # on the ranking algorithm.
    # 		self.write_edge_contexts(papers, citation_edges, ctxs_file)

    # Writes the gexf
    #		write_graph(graph, model_file)
    return graph


if __name__ == '__main__':
  log.basicConfig(format='%(asctime)s [%(levelname)s] : %(message)s', level=log.INFO)
  mb = ModelBuilder()
  graph = mb.build_full_graph()


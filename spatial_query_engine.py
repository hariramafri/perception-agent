import json
import argparse
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
from telemetry import TelemetryAgent
import concurrent.futures

def load_data(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data.get('elements', [])

def cluster_elements(elements, eps=150, min_samples=1):
    """
    Cluster elements based on spatial proximity (x, y coordinates).
    eps: The maximum pixel distance between elements to be grouped together.
    """
    if not elements:
        return []
        
    # Extract centers of bounding boxes for spatial clustering
    coords = []
    for el in elements:
        bbox = el['bounding_box']
        # Calculate the center point of the element
        center_x = bbox['x'] + (bbox['width'] / 2)
        center_y = bbox['y'] + (bbox['height'] / 2)
        coords.append([center_x, center_y])
        
    coords = np.array(coords)
    
    # Apply DBSCAN clustering
    # min_samples=1 means every element gets a cluster, even if it's isolated
    clustering = DBSCAN(eps=eps, min_samples=min_samples).fit(coords)
    labels = clustering.labels_
    
    clusters = {}
    for idx, label in enumerate(labels):
        if label not in clusters:
            clusters[label] = []
        clusters[label].append(elements[idx])
        
    return list(clusters.values())

def generate_cluster_summaries(clusters):
    """
    Create a combined text representation for each cluster.
    """
    summaries = []
    for cluster in clusters:
        texts = []
        for el in cluster:
            # Combine inner text and key attributes
            text_parts = []
            if el.get('inner_text'):
                text_parts.append(el['inner_text'])
                
            attrs = el.get('attributes', {})
            for attr in ['aria-label', 'name', 'id', 'class']:
                if attrs.get(attr):
                    text_parts.append(attrs[attr])
                
            combined = " ".join(text_parts)
            if combined.strip():
                texts.append(f"{el['tag_name']}: {combined}")
            else:
                texts.append(f"{el['tag_name']} element")
                
        # Join all elements in the cluster into one descriptive string
        cluster_text = " | ".join(texts)
        summaries.append({
            "cluster_elements": cluster,
            "text_representation": cluster_text
        })
    return summaries

class QueryEngine:
    def __init__(self, model_name='all-MiniLM-L6-v2'):
        """
        Initialize the NLP model. all-MiniLM-L6-v2 is lightweight and very fast for sentence embeddings.
        """
        print(f"Loading NLP model: {model_name}... (This might take a moment to download on first run)")
        self.model = SentenceTransformer(model_name)
        self.cluster_summaries = []
        self.embeddings = None
        
    def fit(self, cluster_summaries):
        """
        Generate vector embeddings for all spatial clusters on the page.
        """
        self.cluster_summaries = cluster_summaries
        texts = [c['text_representation'] for c in cluster_summaries]
        if texts:
            print(f"Generating embeddings for {len(texts)} clusters in parallel...")
            chunk_size = max(1, len(texts) // 4) # Split into 4 chunks minimum or use smaller
            text_chunks = [texts[i:i + chunk_size] for i in range(0, len(texts), chunk_size)]
            
            embeddings_list = []
            with concurrent.futures.ThreadPoolExecutor() as executor:
                results = executor.map(self.model.encode, text_chunks)
                for res in results:
                    embeddings_list.extend(res)
                    
            self.embeddings = np.array(embeddings_list)
        else:
            self.embeddings = []
            
    def query(self, text_query, top_k=1):
        """
        Find the most semantically similar cluster using Cosine Similarity.
        """
        if len(self.cluster_summaries) == 0:
            return []
            
        # Encode the natural language query
        query_embedding = self.model.encode([text_query])
        
        # Calculate cosine similarity between query and all clusters
        similarities = cosine_similarity(query_embedding, self.embeddings)[0]
        
        # Get top k matches
        top_indices = np.argsort(similarities)[::-1][:top_k]
        
        results = []
        for idx in top_indices:
            results.append({
                "score": float(similarities[idx]),
                "cluster": self.cluster_summaries[idx]
            })
            
        return results

def main():
    parser = argparse.ArgumentParser(description="Spatial Clustering and Query Engine for DOM Elements")
    parser.add_argument("--input", "-i", default="output/dom_observation.json", help="Path to JSON file")
    parser.add_argument("--query", "-q", required=True, help="Text query to search for (e.g. 'Click the login button')")
    args = parser.parse_args()
    
    print(f"Loading data from {args.input}...")
    try:
        elements = load_data(args.input)
    except FileNotFoundError:
        print(f"Error: Could not find {args.input}. Run the scraper first.")
        return
        
    print(f"Loaded {len(elements)} elements.")
    
    telemetry = TelemetryAgent("output")
    try:
        with telemetry.track_action("Semantic clustering"):
            # 1. Cluster spatially using DBSCAN
            clusters = cluster_elements(elements, eps=150) # eps is pixel distance
            print(f"Formed {len(clusters)} spatial clusters.")
            
            # 2. Summarize clusters into text strings
            summaries = generate_cluster_summaries(clusters)
            
            # 3. Initialize NLP Engine and embed the clusters
            engine = QueryEngine()
            engine.fit(summaries)
    except Exception as e:
        print(f"Clustering failed: {e}")
        return
        
    # 4. Process the Query
    print(f"\n==============================================")
    print(f"QUERY: '{args.query}'")
    print(f"==============================================\n")
    results = engine.query(args.query, top_k=1)
    
    if results:
        best_match = results[0]
        print(f"Best Match (Confidence Score: {best_match['score']:.4f})\n")
        print(f"Cluster Context: {best_match['cluster']['text_representation']}\n")
        print("Elements in this target cluster:")
        for el in best_match['cluster']['cluster_elements']:
            print(f"  - Tag: <{el['tag_name']}>")
            print(f"    Text: '{el.get('inner_text', '')}'")
            print(f"    Element Index (from JSON): {el['element_index']}")
            print(f"    Coordinates: x={el['bounding_box']['x']}, y={el['bounding_box']['y']}")
            print("")
    else:
        print("No elements found to match against.")

if __name__ == "__main__":
    main()

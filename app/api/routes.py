from flask import Blueprint, request, jsonify, render_template, send_file
from app.core.agent import AgentSystem
from app.core.data_manager import DataManager
from app.core.embeddings import EmbeddingManager
from app.core.windows_agent import WindowsCommandAgent
import json
import datetime
import os
from pathlib import Path

# Create blueprint
api = Blueprint('api', __name__)

# Initialize managers
data_manager = DataManager()
embedding_manager = EmbeddingManager()
agent_system = None
windows_agent = None

def initialize_resources():
    """Initialize all resources needed for the application."""
    global agent_system, windows_agent
    
    # Initialize Windows Command Agent
    windows_agent = WindowsCommandAgent()
    
    # Load FAISS index or create new one
    if not embedding_manager.load_index():
        # Prepare chunks and create new index
        chunk_metadata, chunks = data_manager.prepare_chunks()
        embedding_manager.create_index(chunks)
        
        # Save resources
        embedding_manager.save_index()
        data_manager.save_chunk_metadata()
    else:
        # Load chunk metadata
        data_manager.load_chunk_metadata()
    
    # Initialize agent system
    agent_system = AgentSystem()
    
    # Initialize optimizer and command graph
    agent_system.initialize_optimizer(embedding_manager, data_manager)
    agent_system.initialize_command_graph(data_manager)
    
    return True

@api.route('/')
def home():
    """Render the home page."""
    return render_template('index.html')

@api.route('/api/query', methods=['POST'])
def process_query():
    """API endpoint to process user queries."""
    try:
        data = request.json
        user_query = data.get('query', '')
        
        if not user_query:
            return jsonify({'error': 'No query provided'}), 400
        
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        # Check if it's a Windows command query
        if windows_agent.is_windows_query(user_query):
            try:
                response = windows_agent.process_query(user_query)
                
                if 'error' in response:
                    return jsonify({'error': response['error']}), 500
                
                # Create detailed JSON result for Windows commands
                json_result = {
                    "metadata": {
                        "timestamp": timestamp,
                        "query_id": f"query_{timestamp}",
                        "original_query": user_query,
                        "query_type": "windows_command",
                        "platform": "Windows"
                    },
                    "response": response['response'],
                    "source": "gemini",
                    "is_windows_command": True
                }
                
                # Save to JSON file
                results_dir = Path("query_results")
                results_dir.mkdir(exist_ok=True)
                json_file = results_dir / f"query_{timestamp}.json"
                
                with open(json_file, 'w') as f:
                    json.dump(json_result, f, indent=2)
                
                # Save to text file for logging
                with open("query_results.txt", "a") as f:
                    f.write(f"[{timestamp}] Windows Query: {user_query} | Response: {response['response']}\n\n")
                
                # Return the response with metadata
                response_data = {
                    'response': response['response'],
                    'query_id': f"query_{timestamp}",
                    'json_file': f"query_{timestamp}.json",
                    'is_windows_command': True,
                    'platform': 'Windows'
                }
                
                return jsonify(response_data)
            except Exception as e:
                print(f"Error in Windows command processing: {str(e)}")
                return jsonify({'error': f"Error processing Windows command: {str(e)}"}), 500
        
        # If not a Windows query, proceed with the existing Unix command processing
        # Step 1: Analyze and optimize the query
        analysis = agent_system.query_analyzer_agent(
            user_query,
            embedding_manager=embedding_manager,
            data_manager=data_manager
        )
        
        # Step 2: Retrieve relevant commands
        retrieved_commands, context = agent_system.retrieval_agent(
            analysis.get('reformulated_query', user_query),
            analysis,
            embedding_manager=embedding_manager,
            data_manager=data_manager
        )
        
        if not retrieved_commands:
            return jsonify({'response': 'No relevant commands found. Try rephrasing your query.'})
        
        # Step 3: Generate response
        response = agent_system.response_generator_agent(user_query, analysis, context)
        
        # Step 4: Get command chain recommendations if available
        chain_recommendations = None
        if agent_system.command_graph and retrieved_commands:
            # Use the first command as the starting point
            primary_command = retrieved_commands[0].get('Command', '')
            if primary_command:
                chain_recommendations = agent_system.get_command_chain_recommendations(
                    primary_command,
                    task_description=user_query
                )
        
        # Create detailed JSON result
        json_result = {
            "metadata": {
                "timestamp": timestamp,
                "query_id": f"query_{timestamp}",
                "original_query": user_query,
                "optimized_query": analysis.get('reformulated_query', user_query),
                "query_intent": analysis.get('intent', 'unknown'),
                "keywords": analysis.get('keywords', []),
                "optimization_metrics": analysis.get('optimization_metrics', {})
            },
            "retrieved_commands": [
                {
                    "command": cmd.get('Command', ''),
                    "description": cmd.get('DESCRIPTION', ''),
                    "examples": cmd.get('EXAMPLES', ''),
                    "options": cmd.get('OPTIONS', '')
                } for cmd in retrieved_commands
            ],
            "context": context,
            "response": response,
            "analysis": {
                "query_analysis": analysis,
                "command_relevance": [
                    {
                        "command": cmd.get('Command', ''),
                        "relevance_score": cmd.get('relevance_score', 0)
                    } for cmd in retrieved_commands
                ]
            }
        }
        
        # Add chain recommendations if available
        if chain_recommendations:
            json_result["command_chains"] = chain_recommendations
        
        # Save to JSON file
        results_dir = Path("query_results")
        results_dir.mkdir(exist_ok=True)
        json_file = results_dir / f"query_{timestamp}.json"
        
        with open(json_file, 'w') as f:
            json.dump(json_result, f, indent=2)
        
        # Save to text file for backward compatibility
        with open("query_results.txt", "a") as f:
            f.write(f"[{timestamp}] Query: {user_query} | Response: {response}\n\n")
        
        # Return the response with additional metadata
        response_data = {
            'response': response,
            'analysis': analysis,
            'commands': [cmd.get('Command', '') for cmd in retrieved_commands],
            'query_id': f"query_{timestamp}",
            'json_file': f"query_{timestamp}.json"
        }
        
        # Add chain recommendations to response if available
        if chain_recommendations:
            response_data['command_chains'] = chain_recommendations
            
        return jsonify(response_data)
    
    except Exception as e:
        print(f"Error processing query: {e}")
        return jsonify({'error': str(e)}), 500
    
@api.route('/api/download/<query_id>', methods=['GET'])
def download_query_result(query_id):
    """Download the JSON result for a specific query."""
    try:
        json_file = Path("query_results") / f"{query_id}.json"
        if not json_file.exists():
            return jsonify({'error': 'Query result not found'}), 404
        
        return send_file(
            json_file,
            mimetype='application/json',
            as_attachment=True,
            download_name=f"{query_id}.json"
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500 
    
# Add a new route to visualize the command graph
@api.route('/api/command_graph', methods=['GET'])
def get_command_graph():
    """Generate and return a visualization of the command graph."""
    if not agent_system or not agent_system.command_graph:
        return jsonify({'error': 'Command graph not initialized'}), 500
        
    # Generate visualization
    graph_file = Path("command_graph.png")
    if agent_system.command_graph.visualize_graph(str(graph_file)):
        return send_file(
            graph_file,
            mimetype='image/png',
            as_attachment=False
        )
    else:
        return jsonify({'error': 'Failed to generate graph visualization'}), 500
        
# Add a new route to get command chain recommendations
@api.route('/api/command_chains/<command>', methods=['GET'])
def get_command_chains(command):
    """Get command chain recommendations for a specific command."""
    if not agent_system or not agent_system.command_graph:
        return jsonify({'error': 'Command graph not initialized'}), 500
        
    task_description = request.args.get('task', '')
    
    recommendations = agent_system.get_command_chain_recommendations(
        command,
        task_description=task_description if task_description else None
    )
    
    if not recommendations:
        return jsonify({'error': 'No recommendations found for this command'}), 404
        
    return jsonify(recommendations)

# Add a new route to explain a command
@api.route('/api/explain', methods=['POST'])
def explain_command():
    """API endpoint to explain a given command and provide examples for Unix or Windows."""
    try:
        data = request.json
        command = data.get('command', '')
        if not command:
            return jsonify({'error': 'No command provided'}), 400
        # Determine if it's a Windows command
        is_win = windows_agent.is_windows_query(command)
        if is_win:
            # Delegate to WindowsCommandAgent for strict Windows formatting
            win_resp = windows_agent.process_query(command)
            if 'error' in win_resp:
                return jsonify({'error': win_resp['error']}), 500
            explanation = win_resp.get('response', '')
            platform = 'Windows'
        else:
            # Use the Unix explain command agent
            explanation = agent_system.explain_command_agent(command, data_manager)
            platform = 'Unix'
        # Save to JSON file
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        results_dir = Path("query_results")
        results_dir.mkdir(exist_ok=True)
        result = {
            "metadata": {
                "timestamp": timestamp,
                "command": command,
                "query_type": "explain_command",
                "platform": platform
            },
            "explanation": explanation
        }
        json_file = results_dir / f"explain_{timestamp}.json"
        with open(json_file, 'w') as f:
            json.dump(result, f, indent=2)
        # Log to text file
        with open("query_results.txt", "a") as f:
            f.write(f"[{timestamp}] Explain ({platform}): {command} | Explanation: {explanation}\n\n")
        # Return explanation to client with platform
        return jsonify({'explanation': explanation, 'command': command, 'platform': platform})
    except Exception as e:
        print(f"Error processing explain command: {str(e)}")
        return jsonify({'error': f"Error explaining command: {str(e)}"}), 500
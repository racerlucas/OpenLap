import os
import tkinter as tk
from tkinter import filedialog, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import lap_utils

class LapSplitGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("OpenLap - Manual Lap Splitter")
        self.root.geometry("1000x800")
        
        self.points = []
        self.tree = None
        self.column_names = None
        self.header_lines = None
        self.current_file = None
        
        self.line_center = None
        self.line_p1 = None
        self.line_p2 = None
        self.line_width = 20.0
        self.heading = 0.0
        
        self.setup_ui()
        
    def setup_ui(self):
        # Top Frame
        top_frame = tk.Frame(self.root)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        
        tk.Button(top_frame, text="Open GPX/VBO", command=self.load_file).pack(side=tk.LEFT)
        self.file_label = tk.Label(top_frame, text="No file loaded")
        self.file_label.pack(side=tk.LEFT, padx=10)
        
        # Center Frame (Plot)
        self.fig = Figure(figsize=(8, 6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_aspect('equal')
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)
        self.canvas.mpl_connect('button_press_event', self.on_click)
        
        # Bottom Frame
        bottom_frame = tk.Frame(self.root)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=10)
        
        tk.Label(bottom_frame, text="Width (m):").pack(side=tk.LEFT)
        self.width_slider = tk.Scale(bottom_frame, from_=5, to=100, orient=tk.HORIZONTAL, length=200, command=self.on_width_change)
        self.width_slider.set(20)
        self.width_slider.pack(side=tk.LEFT, padx=5)
        
        self.lap_label = tk.Label(bottom_frame, text="Laps: 0")
        self.lap_label.pack(side=tk.LEFT, padx=20)
        
        tk.Button(bottom_frame, text="Save Processed File", command=self.save_file, bg="red", fg="white").pack(side=tk.RIGHT, padx=10)

    def load_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("GPX/VBO files", "*.gpx *.vbo")])
        if not file_path:
            return
            
        self.current_file = file_path
        self.file_label.config(text=os.path.basename(file_path))
        
        if file_path.lower().endswith('.gpx'):
            self.points, self.tree, self.ns = lap_utils.load_gpx(file_path)
            self.column_names = None
        else:
            self.points, self.column_names, self.header_lines = lap_utils.load_vbo(file_path)
            self.tree = None
            
        self.line_center = None
        self.line_p1 = None
        self.line_p2 = None
        self.plot_trajectory()

    def plot_trajectory(self):
        self.ax.clear()
        if not self.points:
            self.canvas.draw()
            return
            
        lats = [p['lat'] for p in self.points]
        lons = [p['lon'] for p in self.points]
        
        self.ax.plot(lons, lats, color='blue', alpha=0.5, label='Trajectory')
        
        if self.line_p1 and self.line_p2:
            self.ax.plot([self.line_p1['lon'], self.line_p2['lon']], 
                         [self.line_p1['lat'], self.line_p2['lat']], 
                         color='red', linewidth=3, label='Start Line')
            
            # Identify laps and markers
            self.points = lap_utils.split_into_laps(self.points, self.line_p1, self.line_p2)
            crossings = []
            for i in range(1, len(self.points)):
                if self.points[i]['lap'] > self.points[i-1]['lap']:
                    crossings.append(self.points[i])
            
            if crossings:
                cX = [p['lon'] for p in crossings]
                cY = [p['lat'] for p in crossings]
                self.ax.scatter(cX, cY, color='green', marker='X', s=100, label='Lap Marker')
            
            self.lap_label.config(text=f"Laps: {self.points[-1]['lap'] if self.points else 0}")

        self.ax.legend()
        self.canvas.draw()

    def on_click(self, event):
        if event.inaxes != self.ax or not self.points:
            return
            
        # Find nearest point
        min_dist = float('inf')
        nearest_idx = 0
        click_p = {'lat': event.ydata, 'lon': event.xdata}
        
        # Simple distance check (not haversine since we are in plot coord)
        for i, p in enumerate(self.points):
            # Using simple Euclidean for plot interaction responsiveness
            d = (p['lat'] - click_p['lat'])**2 + (p['lon'] - click_p['lon'])**2
            if d < min_dist:
                min_dist = d
                nearest_idx = i
        
        self.line_center = self.points[nearest_idx]
        
        # Calculate heading
        prev_idx = max(0, nearest_idx - 1)
        next_idx = min(len(self.points) - 1, nearest_idx + 1)
        if prev_idx != next_idx:
            self.heading = lap_utils.calculate_bearing(self.points[prev_idx], self.points[next_idx])
        else:
            self.heading = 0
            
        self.update_line()
        self.plot_trajectory()

    def on_width_change(self, val):
        self.line_width = float(val)
        if self.line_center:
            self.update_line()
            self.plot_trajectory()

    def update_line(self):
        if not self.line_center:
            return
        # Reference Dart: P1 = heading - 90, P2 = heading + 90
        half_width = self.line_width / 2.0
        self.line_p1 = lap_utils.offset_point(self.line_center, half_width, self.heading - 90)
        self.line_p2 = lap_utils.offset_point(self.line_center, half_width, self.heading + 90)

    def save_file(self):
        if not self.current_file or not self.line_p1:
            messagebox.showwarning("Warning", "Load a file and set a start line first.")
            return
            
        base_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(base_dir, 'processed_data')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        filename = os.path.basename(self.current_file)
        # Suggest a filename
        save_path = filedialog.asksaveasfilename(
            initialdir=output_dir,
            initialfile=filename,
            filetypes=[("GPX/VBO files", "*.gpx *.vbo")]
        )
        
        if not save_path:
            return
            
        if self.current_file.lower().endswith('.gpx'):
            lap_utils.save_gpx(save_path, self.tree, self.points, self.ns)
        else:
            lap_utils.save_vbo(save_path, self.points, self.column_names, self.header_lines)
            
        messagebox.showinfo("Success", f"File saved to {save_path}")

if __name__ == "__main__":
    root = tk.Tk()
    app = LapSplitGUI(root)
    root.mainloop()

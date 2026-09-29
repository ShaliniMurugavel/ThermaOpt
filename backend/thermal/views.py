import traceback
from rest_framework import viewsets, status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from .models import ClimateData, Material, InsulationMaterial, ShelterDesign, Simulation, SimulationResult
from .serializers import (
    ClimateDataSerializer, MaterialSerializer, InsulationMaterialSerializer,
    ShelterDesignSerializer, SimulationSerializer, SimulationRequestSerializer
)
from .thermal_engine import run_simulation

from django.http import JsonResponse
from django.db import connection


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        return JsonResponse({
            "status": "ok",
            "message": "ThermaOpt backend is running.",
            "database": "connected"
        })

    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": "Database connection failed.",
            "error": str(e)
        }, status=500)
        
class ClimateDataViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ClimateData.objects.all()
    serializer_class = ClimateDataSerializer

    def list(self, request, *args, **kwargs):
        try:
            return super().list(request, *args, **kwargs)
        except Exception as e:
            print("===== CLIMATE API ERROR =====")
            print(f"Error type: {type(e).__name__}")
            print(f"Error message: {str(e)}")
            traceback.print_exc()
            print("===== END CLIMATE API ERROR =====")

            return Response(
                {"error": "Climate API failed. Check server logs."},
                status=500
            )

class MaterialViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Material.objects.all()
    serializer_class = MaterialSerializer

class InsulationMaterialViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = InsulationMaterial.objects.all()
    serializer_class = InsulationMaterialSerializer

class ShelterDesignViewSet(viewsets.ModelViewSet):
    queryset = ShelterDesign.objects.all()
    serializer_class = ShelterDesignSerializer

class SimulationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Simulation.objects.all()
    serializer_class = SimulationSerializer

@api_view(['POST'])
def simulate_endpoint(request):
    """
    POST /api/simulate/
    Payload: { "climate_id": 1, "design_id": 1 }
    """
    serializer = SimulationRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
    climate_id = serializer.validated_data['climate_id']
    design_id = serializer.validated_data['design_id']
    
    climate = get_object_or_404(ClimateData, id=climate_id)
    design = get_object_or_404(ShelterDesign, id=design_id)
    
    # Check if wall/roof material exists
    if not design.wall_material or not design.roof_material:
        return Response({"error": "Design must have wall and roof materials."}, status=status.HTTP_400_BAD_REQUEST)
        
    # Map Django models to dictionaries for the engine
    climate_dict = {
        'avg_temperature_c': climate.avg_temperature_c,
        'solar_radiation_wm2': climate.solar_radiation_wm2,
        'wind_speed_ms': climate.wind_speed_ms
    }
    
    shelter_dict = {
        'shape': design.shape,
        'length': design.length,
        'width': design.width,
        'height': design.height,
        'orientation': design.orientation,
        'window_area': design.window_area,
        'door_area': design.door_area,
        'wall_material': {
            'thermal_conductivity': design.wall_material.thermal_conductivity,
            'density': design.wall_material.density,
            'specific_heat': design.wall_material.specific_heat
        },
        'roof_material': {
            'thermal_conductivity': design.roof_material.thermal_conductivity,
            'density': design.roof_material.density,
            'specific_heat': design.roof_material.specific_heat
        },
        'insulation': {
            'r_value_per_inch': design.insulation.r_value_per_inch
        } if design.insulation else None,
        'insulation_thickness': design.insulation_thickness
    }
    
    # 3. Run the thermal engine
    results_dict = run_simulation(climate_dict, shelter_dict)
    
    # 5. Create Simulation record
    simulation = Simulation.objects.create(
        climate=climate,
        design=design
    )
    
    # 6. Create SimulationResult record
    result_record = SimulationResult.objects.create(
        simulation=simulation,
        indoor_temperature_c=results_dict['indoor_temperature_c'],
        heat_loss_w=results_dict['heat_loss_w'],
        solar_heat_gain_w=results_dict['solar_heat_gain_w'],
        heating_requirement_kwh=results_dict['heating_requirement_kwh'],
        thermal_comfort_score=results_dict['thermal_comfort_score']
    )
    
    # 7. Return the calculated result as JSON
    response_data = SimulationSerializer(simulation).data
    return Response(response_data, status=status.HTTP_201_CREATED)

@api_view(['POST'])
def optimize_endpoint(request):
    """
    POST /api/optimize/
    Payload: {
        "base_design_id": 1,
        "climate_id": 1,
        "variations": [
            { "name": "Option A", "overrides": {} },
            { "name": "Option B", "overrides": { "insulation_thickness": 5.0, "orientation": 90 } },
            { "name": "Option C", "overrides": { "wall_material": 2 } }
        ]
    }
    """
    from .thermal_engine.optimizer import compare_designs
    
    data = request.data
    base_design_id = data.get('base_design_id')
    climate_id = data.get('climate_id')
    variations = data.get('variations', [])
    
    if not all([base_design_id, climate_id, variations]):
        return Response({"error": "Missing required fields."}, status=status.HTTP_400_BAD_REQUEST)
        
    climate = get_object_or_404(ClimateData, id=climate_id)
    base_design = get_object_or_404(ShelterDesign, id=base_design_id)
    
    climate_dict = {
        'avg_temperature_c': climate.avg_temperature_c,
        'solar_radiation_wm2': climate.solar_radiation_wm2,
        'wind_speed_ms': climate.wind_speed_ms
    }
    
    results = []
    
    for var in variations:
        overrides = var.get('overrides', {})
        
        # Resolve materials if overridden
        wall_mat_id = overrides.get('wall_material', base_design.wall_material.id if base_design.wall_material else None)
        roof_mat_id = overrides.get('roof_material', base_design.roof_material.id if base_design.roof_material else None)
        insul_id = overrides.get('insulation', base_design.insulation.id if base_design.insulation else None)
        
        wall_mat = get_object_or_404(Material, id=wall_mat_id) if wall_mat_id else base_design.wall_material
        roof_mat = get_object_or_404(Material, id=roof_mat_id) if roof_mat_id else base_design.roof_material
        insul = get_object_or_404(InsulationMaterial, id=insul_id) if insul_id else base_design.insulation
        
        if not wall_mat or not roof_mat:
            return Response({"error": "Design must have wall and roof materials."}, status=status.HTTP_400_BAD_REQUEST)
            
        shelter_dict = {
            'shape': overrides.get('shape', base_design.shape),
            'length': float(overrides.get('length', base_design.length)),
            'width': float(overrides.get('width', base_design.width)),
            'height': float(overrides.get('height', base_design.height)),
            'orientation': float(overrides.get('orientation', base_design.orientation)),
            'window_area': float(overrides.get('window_area', base_design.window_area)),
            'door_area': float(overrides.get('door_area', base_design.door_area)),
            'wall_material': {
                'thermal_conductivity': wall_mat.thermal_conductivity,
                'density': wall_mat.density,
                'specific_heat': wall_mat.specific_heat
            },
            'roof_material': {
                'thermal_conductivity': roof_mat.thermal_conductivity,
                'density': roof_mat.density,
                'specific_heat': roof_mat.specific_heat
            },
            'insulation': {
                'r_value_per_inch': insul.r_value_per_inch
            } if insul else None,
            'insulation_thickness': float(overrides.get('insulation_thickness', base_design.insulation_thickness))
        }
        
        # Run engine
        sim_result = run_simulation(climate_dict, shelter_dict)
        sim_result['configuration_name'] = var.get('name', 'Unknown Option')
        sim_result['overrides'] = overrides
        results.append(sim_result)
        
    best_result = compare_designs(results)
    
    return Response({
        "results": results,
        "best_configuration": best_result['configuration_name'] if best_result else None
    }, status=status.HTTP_200_OK)

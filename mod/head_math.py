"""Camera-local rotation: +yaw right, +pitch up, +roll right lean; -Z forward."""
import math
import struct
LIMITS=(20.0,10.0,5.0)
PHYSICAL_LIMITS=(90.0,45.0,20.0)

def validate_angles(angles,limits=LIMITS):
    if tuple(limits) not in (LIMITS,PHYSICAL_LIMITS):raise ValueError('Unknown rotation envelope')
    if len(angles)!=3 or any(not math.isfinite(v) or abs(v)>limit for v,limit in zip(angles,limits)):
        raise ValueError('D7 rotation bounds exceeded')
    return tuple(float(v) for v in angles)

def multiply(a,b):
    w,x,y,z=a;v,i,j,k=b
    return (w*v-x*i-y*j-z*k,w*i+x*v+y*k-z*j,w*j-x*k+y*v+z*i,w*k+x*j-y*i+z*v)

def axis_quaternion(axis,degrees):
    angle=math.radians(degrees)/2;scale=math.sin(angle)
    return (math.cos(angle),*(v*scale for v in axis))

def head_quaternion(angles,limits=LIMITS):
    yaw,pitch,roll=validate_angles(angles,limits)
    # Local +X right, +Y up, -Z view-forward, qualified by H3 screenshots.
    q=multiply(multiply(axis_quaternion((0,1,0),-yaw),axis_quaternion((1,0,0),pitch)),axis_quaternion((0,0,1),-roll))
    length=math.sqrt(sum(v*v for v in q))
    return tuple(v/length for v in q)

def rotation_columns(q):
    w,x,y,z=q
    return ((1-2*(y*y+z*z),2*(x*y+w*z),2*(x*z-w*y)),
            (2*(x*y-w*z),1-2*(x*x+z*z),2*(y*z+w*x)),
            (2*(x*z+w*y),2*(y*z-w*x),1-2*(x*x+y*y)))

def identity_slerp(q,weight):
    """Shortest-arc spherical blend from identity to q; no extra tracking filter."""
    if not math.isfinite(weight) or not 0<=weight<=1:raise ValueError('Invalid rotation weight')
    if q[0]<0:q=tuple(-v for v in q)
    theta=math.acos(max(-1,min(1,q[0])))
    if theta<1e-9:return (1.,0.,0.,0.)
    scale=math.sin(weight*theta)/math.sin(theta)
    return (math.cos(weight*theta),*(v*scale for v in q[1:]))

def compose_basis(original,angles,weight=1,limits=LIMITS):
    if len(original)!=48:raise ValueError('Expected three native 16-byte axis lanes')
    angles=validate_angles(angles,limits)
    if not math.isfinite(weight) or not 0<=weight<=1:raise ValueError('Invalid rotation weight')
    if angles==(0.0,0.0,0.0) or weight==0:return original
    axes=[struct.unpack_from('<3f',original,i*16) for i in range(3)]
    if not all(math.isfinite(v) for a in axes for v in a):raise ValueError('Nonfinite native basis')
    dot=lambda a,b:sum(x*y for x,y in zip(a,b))
    error=max(abs(dot(a,b)-(1 if i==j else 0)) for i,a in enumerate(axes) for j,b in enumerate(axes))
    determinant=dot(axes[0],(axes[1][1]*axes[2][2]-axes[1][2]*axes[2][1],axes[1][2]*axes[2][0]-axes[1][0]*axes[2][2],axes[1][0]*axes[2][1]-axes[1][1]*axes[2][0]))
    if error>.002 or determinant<.998:raise ValueError('Unqualified native rotation basis')
    columns=rotation_columns(identity_slerp(head_quaternion(angles,limits),weight));result=bytearray(original)
    for axis,column in enumerate(columns):
        new=[sum(axes[i][j]*column[i] for i in range(3)) for j in range(3)]
        struct.pack_into('<3f',result,axis*16,*new)
    return bytes(result)

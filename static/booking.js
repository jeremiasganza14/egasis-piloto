'use strict';
const query=new URLSearchParams(location.search),token=query.get('token'),workspace_id=Number(query.get('workspace_id')),contact_id=Number(query.get('contact_id'));
const info=document.getElementById('booking-info'),slots=document.getElementById('slots'),error=document.getElementById('booking-error'),title=document.getElementById('booking-title');
let invitation;
function label(start){return new Date(start*1000).toLocaleString('es-AR',{timeZone:invitation.timezone,weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});}
function button(text,handler,primary=false){const b=document.createElement('button');b.type='button';b.className=primary?'primary':'secondary';b.textContent=text;b.addEventListener('click',handler);slots.append(b);}
function confirmed(start){title.textContent='Tu reunión quedó confirmada.';info.textContent=`${label(start)} · ${invitation.duration_minutes} minutos · ${invitation.timezone}. El equipo tiene tu reserva registrada en Egasis.`;slots.replaceChildren();}
async function available(){
 error.textContent='';
 try{
  const response=await fetch('/api/booking?'+query.toString());const data=await response.json();if(!response.ok)throw new Error(data.detail||'No pudimos consultar la invitación.');invitation=data;slots.replaceChildren();
  if(data.reservation){if(data.reservation.status==='confirmed')confirmed(data.reservation.starts_at);else {title.textContent='Estamos verificando tu reserva.';info.textContent=`${label(data.reservation.starts_at)} · El equipo revisará la confirmación del calendario. No necesitás reservar otra vez.`;}return;}
  title.textContent='Conversá con '+data.business+'.';info.textContent=`Elegí un horario y confirmalo en el próximo paso. Reunión de ${data.duration_minutes} minutos · Horarios en ${data.timezone}`;
  for(const start of data.slots)button(label(start),()=>selectSlot(start));
  if(!data.slots.length)info.textContent='No hay horarios disponibles en los próximos días. Contactá al equipo para coordinar.';
 }catch(e){error.textContent=e.message;}
}
function selectSlot(start){title.textContent='Confirmá tu reunión.';info.textContent=`${label(start)} · ${invitation.duration_minutes} minutos · ${invitation.timezone}`;slots.replaceChildren();button('Confirmar reunión',()=>reserve(start),true);button('Elegir otro horario',available);}
async function reserve(starts_at){
 slots.querySelectorAll('button').forEach(b=>b.disabled=true);error.textContent='';
 try{const response=await fetch('/api/booking',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id,contact_id,token,starts_at})});const data=await response.json();if(!response.ok)throw new Error(data.detail||'No pudimos confirmar la reserva.');confirmed(starts_at);}
 catch(e){error.textContent=e.message;slots.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
available();
